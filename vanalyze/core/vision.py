"""画面关键帧抽取与画面文字识别（本地 OCR）。

- 抽帧：PyAV 解码（Windows 中文路径安全），按时长均匀采样 + 场景变化检测，
  dHash 去重，最多 ``max_frames`` 张，写入 ``out_dir/f%04d.jpg``（宽 ≤1280）；
- OCR：rapidocr（2.x/3.x API 兼容），逐帧识别，行文本用 `` / `` 连接；
- 可选视觉 LLM 描述（``settings.vision_enabled`` 默认关闭），失败返回 ``[]``；
- 抽帧 / OCR 失败不抛错（pipeline 降级为空 ocr.json 继续）。
"""

from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np

from ..models import OcrFrame, OcrResult, ProgressFn
from . import _media

logger = logging.getLogger(__name__)

_MAX_FRAME_WIDTH = 1280
#: 送 OCR 前缩小的宽度（检测/识别输入，仅为提速；保存到磁盘的帧仍保持 ≤1280 宽）
_OCR_MAX_WIDTH = 960
#: 抽帧数量硬上限（settings.max_frames 默认 100，这里代码内再收紧，控制 OCR 总耗时）
MAX_FRAMES_HARD_LIMIT = 60
_SCENE_HIST_THRESHOLD = 0.05
_SCENE_MAX_SAMPLES = 200
_DUP_HIST_DISTANCE = 0.03
_OCR_TEXT_LIMIT = 800
_OCR_LINE_SEPARATOR = " / "
_OCR_PROGRESS_STEP = 1

_OCR_LOCK = threading.RLock()
_OCR_ENGINE: Any = None
_OCR_IMPORT_TRIED = False
#: rapidocr 是否支持 use_cls 等快速参数（旧版 API 不支持时自动回退）
_OCR_FAST_ARGS: Optional[bool] = None


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _emit(progress: Optional[ProgressFn], stage: str, pct: float, message: Optional[str]) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0.0, min(1.0, float(pct))), message)
    except Exception:  # pragma: no cover
        logger.debug("progress 回调异常", exc_info=True)


def format_timestamp(seconds: float) -> str:
    """把秒数格式化为 ``MM:SS``（超过 1 小时时分钟数继续累加）。"""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    minutes, sec = divmod(total, 60)
    return f"{minutes:02d}:{sec:02d}"


def _dhash(gray: np.ndarray) -> np.ndarray:  # pragma: no cover - 保留工具函数
    """64 位 dHash（9x8 灰度差分）。"""
    import cv2

    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return (small[:, 1:] > small[:, :-1]).flatten()


def _hamming(a: np.ndarray, b: np.ndarray) -> int:  # pragma: no cover
    return int(np.count_nonzero(a != b))


def _gray_hist(gray: np.ndarray) -> Optional[np.ndarray]:
    """64x36 灰度直方图（用于去重/场景变化判断，对文字变化比 dHash 敏感）。"""
    import cv2

    try:
        small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        hist = cv2.calcHist([small], [0], None, [64], [0, 256])
        cv2.normalize(hist, hist)
        return hist
    except Exception:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# 候选时间点
# ---------------------------------------------------------------------------
def _even_select(values: List[float], count: int) -> List[float]:
    if count <= 0 or not values:
        return []
    if len(values) <= count:
        return list(values)
    indices = np.linspace(0, len(values) - 1, count)
    picked = sorted({values[int(round(i))] for i in indices})
    return picked


def _candidate_times(reader: "_media.VideoReader", duration: float, interval: float, max_frames: int) -> List[float]:
    """均匀采样 + 场景变化检测，返回候选时间点（秒，升序去重）。"""
    import cv2

    dur = float(duration or reader.duration or 0.0)
    times: List[float] = []
    if dur <= 0:
        return [0.0]

    interval = max(0.5, float(interval or 20.0))
    # 均匀采样 + 首帧 + 末帧；短视频自动加密采样，避免漏掉幻灯片等静态画面切换
    step = interval
    if dur / step < 6:
        step = max(0.5, dur / 6.0)
    t = 0.0
    while t < max(0.0, dur - 0.05):
        times.append(round(t, 3))
        t += step
    times.append(round(max(0.0, dur - 0.15), 3))

    # 场景变化检测（最多 _SCENE_MAX_SAMPLES 个采样点，控制解码次数）
    scene_step = max(1.0, dur / float(_SCENE_MAX_SAMPLES))
    sample_count = int(dur / scene_step) + 1
    sample_count = max(2, min(_SCENE_MAX_SAMPLES, sample_count))
    scene_times: List[float] = []
    previous_hist = None
    for index in range(sample_count):
        at = min(dur - 0.05, index * scene_step)
        try:
            frame = reader.frame_at(at)
        except Exception:  # pragma: no cover - 单帧解码失败不影响整体
            frame = None
        if frame is None:
            continue
        try:
            hist = _gray_hist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        except Exception:  # pragma: no cover
            hist = None
        if hist is None:
            previous_hist = None
            continue
        if previous_hist is not None:
            try:
                diff = float(cv2.compareHist(previous_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
            except Exception:  # pragma: no cover
                diff = 0.0
            if diff >= _SCENE_HIST_THRESHOLD:
                scene_times.append(round(float(at), 3))
        previous_hist = hist

    merged: List[float] = []
    for at in sorted(set(times + scene_times)):
        if not merged or at - merged[-1] >= 1.0:
            merged.append(at)
    return _even_select(merged, max(1, int(max_frames)))


# ---------------------------------------------------------------------------
# 抽帧
# ---------------------------------------------------------------------------
def extract_frames(
    media_path: str,
    out_dir: str,
    *,
    max_frames: int,
    interval: float,
    duration: float = 0.0,
    progress: Optional[ProgressFn] = None,
) -> List[Tuple[float, str]]:
    """抽取关键帧，返回 ``[(秒, 帧绝对路径)]``；失败返回 ``[]``（不抛错）。"""
    frames: List[Tuple[float, str]] = []
    try:
        out_path = Path(str(out_dir))
        out_path.mkdir(parents=True, exist_ok=True)
        max_frames = max(1, min(int(max_frames or 100), MAX_FRAMES_HARD_LIMIT))
        interval = max(0.5, float(interval or 20.0))

        _emit(progress, "frames", 0.01, "分析视频画面…")
        with _media.VideoReader(media_path) as reader:
            total_duration = float(duration or reader.duration or 0.0)
            times = _candidate_times(reader, total_duration, interval, max_frames)
            if not times:
                _emit(progress, "frames", 1.0, "未找到可抽取的画面")
                return []
            _emit(progress, "frames", 0.05, f"共选取 {len(times)} 个采样点")

            count = 0
            last_hists: List[np.ndarray] = []
            total = len(times)
            for index, at in enumerate(times):
                if count >= max_frames:
                    break
                try:
                    frame = reader.frame_at(at)
                except Exception:  # pragma: no cover
                    frame = None
                if frame is None:
                    continue
                try:
                    import cv2

                    hist = _gray_hist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
                except Exception:  # pragma: no cover
                    hist = None
                if hist is not None and last_hists:
                    try:
                        duplicate = any(
                            float(cv2.compareHist(hist, kept, cv2.HISTCMP_BHATTACHARYYA))
                            < _DUP_HIST_DISTANCE
                            for kept in last_hists[-8:]
                        )
                    except Exception:  # pragma: no cover
                        duplicate = False
                    if duplicate and frames:
                        continue
                resized = _media.resize_width(frame, _MAX_FRAME_WIDTH)
                frame_path = out_path / f"f{count + 1:04d}.jpg"
                if not _media.write_jpeg(frame_path, resized, quality=82):
                    continue
                frames.append((round(float(at), 3), str(frame_path)))
                if hist is not None:
                    last_hists.append(hist)
                count += 1
                pct = 0.05 + 0.93 * (index + 1) / max(1, total)
                _emit(progress, "frames", pct, f"抽取画面关键帧 {count}/{max_frames}")
        _emit(progress, "frames", 1.0, f"已抽取 {len(frames)} 帧")
        logger.info("抽帧完成：%s 帧（%s）", len(frames), media_path)
    except Exception as exc:
        logger.warning("抽帧失败（降级为无关键帧）：%s", exc)
        return frames
    return frames


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def _build_ocr_engine() -> Any:
    """初始化 rapidocr 引擎（进程内缓存）。兼容 3.x / 2.x / rapidocr_onnxruntime。"""
    for name in ("RapidOCR", "rapidocr", "rapidocr_onnxruntime"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        from rapidocr import RapidOCR  # type: ignore

        try:
            engine = RapidOCR(params={"Global.log_level": "ERROR"})
        except Exception:
            engine = RapidOCR()
        logger.info("RapidOCR 已加载（rapidocr 新版 API）")
        return engine
    except Exception as exc:
        logger.debug("from rapidocr import RapidOCR 失败：%s", exc)
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        engine = RapidOCR()
        logger.info("RapidOCR 已加载（rapidocr_onnxruntime 旧版 API）")
        return engine
    except Exception as exc:
        logger.warning("RapidOCR 不可用，画面文字识别将被跳过：%s", exc)
        return None


def _get_ocr_engine() -> Any:
    global _OCR_ENGINE, _OCR_IMPORT_TRIED
    if _OCR_IMPORT_TRIED:
        return _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_IMPORT_TRIED:
            return _OCR_ENGINE
        _OCR_IMPORT_TRIED = True
        try:
            _OCR_ENGINE = _build_ocr_engine()
        except Exception as exc:  # pragma: no cover - 兜底
            logger.warning("初始化 OCR 引擎失败：%s", exc)
            _OCR_ENGINE = None
    return _OCR_ENGINE


def _ocr_items(result: Any) -> List[Tuple[str, float]]:
    """把不同版本 rapidocr 的输出统一成 [(text, score)]。"""
    if result is None:
        return []
    txts = getattr(result, "txts", None)
    if txts is not None:
        scores = getattr(result, "scores", None) or []
        items: List[Tuple[str, float]] = []
        for index, text in enumerate(txts):
            try:
                score = float(scores[index]) if index < len(scores) else 0.0
            except (TypeError, ValueError):
                score = 0.0
            items.append((str(text), score))
        return items
    # 旧版：(([[box, text, score], ...]), elapsed)
    if isinstance(result, (tuple, list)) and result:
        payload = result[0]
        if payload is None:
            return []
        items = []
        for line in payload:
            if isinstance(line, (list, tuple)) and len(line) >= 2:
                text = line[1]
                score = line[2] if len(line) > 2 else 0.0
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 0.0
                items.append((str(text), score))
        return items
    if isinstance(result, dict):
        texts = result.get("txts") or result.get("text") or []
        scores = result.get("scores") or []
        items = []
        for index, text in enumerate(texts):
            try:
                score = float(scores[index]) if index < len(scores) else 0.0
            except (TypeError, ValueError):
                score = 0.0
            items.append((str(text), score))
        return items
    return []


def _call_ocr_engine(engine: Any, image: Optional[np.ndarray], frame_path: str) -> Any:
    """调用 OCR 引擎；优先关闭方向分类器提速，旧版 API 不支持时自动回退。"""
    global _OCR_FAST_ARGS
    target: Any = image if image is not None else str(frame_path)
    if _OCR_FAST_ARGS is not False:
        try:
            result = engine(target, use_cls=False)
            _OCR_FAST_ARGS = True
            return result
        except TypeError:  # 旧版 rapidocr 不支持关键字参数
            _OCR_FAST_ARGS = False
    return engine(target)


def _recognize(engine: Any, frame_path: str) -> Tuple[str, float]:
    """单帧 OCR，返回 (文本, 平均置信度)。识别前缩放到宽 ≤960 以提速。"""
    image = _media.read_image_bgr(frame_path)
    if image is not None:
        image = _media.resize_width(image, _OCR_MAX_WIDTH)
    try:
        result = _call_ocr_engine(engine, image, frame_path)
    except Exception as exc:
        logger.debug("OCR 识别失败 %s：%s", frame_path, exc)
        return "", 0.0

    items = [(text.strip(), score) for text, score in _ocr_items(result) if text and text.strip()]
    if not items:
        return "", 0.0
    text = _OCR_LINE_SEPARATOR.join(text for text, _ in items)[:_OCR_TEXT_LIMIT]
    scores = [score for _, score in items if 0.0 <= score <= 1.0]
    average = float(sum(scores) / len(scores)) if scores else 0.0
    return text, average


def run_ocr(
    frames: List[Tuple[float, str]],
    *,
    progress: Optional[ProgressFn] = None,
) -> OcrResult:
    """逐帧识别画面文字，返回 :class:`OcrResult`。"""
    result = OcrResult()
    if not frames:
        _emit(progress, "ocr", 1.0, "没有可识别的画面帧")
        return result

    engine = _get_ocr_engine()
    if engine is None:
        _emit(progress, "ocr", 1.0, "OCR 引擎不可用，跳过画面文字识别")
        return result

    total = len(frames)
    for index, (at, frame_path) in enumerate(frames):
        text, score = _recognize(engine, frame_path)
        if text:
            result.frames.append(
                OcrFrame(
                    time=round(float(at), 3),
                    text=text,
                    frame=Path(frame_path).name,
                    score=round(float(score), 4),
                )
            )
        if index % _OCR_PROGRESS_STEP == 0:
            _emit(progress, "ocr", (index + 1) / max(1, total), f"识别画面文字 {index + 1}/{total}")

    result.frames.sort(key=lambda item: item.time)
    result.text = "\n".join(
        f"[{format_timestamp(item.time)}] {item.text}" for item in result.frames
    )
    _emit(progress, "ocr", 1.0, f"画面文字识别完成（{len(result.frames)} 帧含文字）")
    logger.info("OCR 完成：%s 帧含文字 / 共 %s 帧", len(result.frames), total)
    return result


# ---------------------------------------------------------------------------
# 可选视觉 LLM 描述（默认关闭）
# ---------------------------------------------------------------------------
def _image_data_url(frame_path: str, max_width: int = 1024) -> Optional[str]:
    try:
        import cv2

        image = _media.read_image_bgr(frame_path)
        if image is None:
            return None
        image = _media.resize_width(image, max_width)
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return None
        encoded = base64.b64encode(buffer.tobytes()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:  # pragma: no cover
        return None


def describe_frames(
    frames: List[Tuple[float, str]],
    settings,
    *,
    progress: Optional[ProgressFn] = None,
) -> List[str]:
    """调用 OpenAI 兼容视觉接口描述画面（默认关闭；失败返回 []）。"""
    if not frames or not bool(getattr(settings, "vision_enabled", False)):
        return []
    base_url = str(getattr(settings, "vision_base_url", "") or "").strip().rstrip("/")
    model = str(getattr(settings, "vision_model", "") or "").strip()
    api_key = str(getattr(settings, "vision_api_key", "") or "").strip()
    if not (base_url and model and api_key):
        logger.info("视觉模型未配置完整，跳过画面描述")
        return []

    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        logger.warning("httpx 不可用，跳过画面描述：%s", exc)
        return []

    # 控制成本：最多 30 帧、每 3 帧一组
    selected = list(frames)
    if len(selected) > 30:
        step = len(selected) / 30.0
        selected = [selected[int(index * step)] for index in range(30)]
    groups = [selected[index:index + 3] for index in range(0, len(selected), 3)]
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    notes: List[str] = []
    timeout = httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=15.0)

    for index, group in enumerate(groups):
        content: List[dict] = [
            {
                "type": "text",
                "text": "以下是同一个视频按时间顺序的截图。请用一句简体中文概括画面中重要的文字或视觉信息（不超过 80 字），不要臆测。",
            }
        ]
        for _, frame_path in group:
            data_url = _image_data_url(frame_path)
            if data_url:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        if len(content) <= 1:
            continue
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": 300,
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                logger.warning("视觉模型请求失败（HTTP %s）", response.status_code)
                continue
            data = response.json()
            choices = data.get("choices") or []
            text = ""
            if choices:
                message = choices[0].get("message") or {}
                text = str(message.get("content") or "").strip()
            if text:
                notes.append(text)
        except Exception as exc:  # pragma: no cover - 视觉为可选能力
            logger.warning("视觉模型调用失败：%s", exc)
            continue
        logger.debug("视觉模型已描述 %s/%s 组画面", index + 1, len(groups))

    return notes
