"""作业处理流水线：串起 解析来源 → 读取媒体 → 缩略图 → 转写 → 抽帧 → OCR → 总结。

产物（契约 §5）：
- ``media.json`` / ``transcript.json`` / ``ocr.json`` / ``summary.json`` / ``thumbnail.jpg``
- ``frames/f0001.jpg`` …（关键帧，可缺失）

失败语义：ASR 失败必须抛 :class:`PipelineError`；关键帧/OCR/总结均可降级，不阻断作业。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import MediaInfo, OcrResult, ProgressFn, Summary, Transcript
from . import analyze, asr, ingest, vision

logger = logging.getLogger(__name__)

_VALID_SOURCE_TYPES = ("file", "url", "tab")


class PipelineError(Exception):
    """流水线错误；``str(exc)`` 为可直接展示给用户的中文消息。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _emit(progress: Optional[ProgressFn], stage: str, pct: float, message: Optional[str]) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0.0, min(1.0, float(pct))), message)
    except Exception:  # pragma: no cover - 回调异常不能影响流水线
        logger.debug("progress 回调异常", exc_info=True)


def _dump_json(model) -> str:
    """把 pydantic 模型序列化为 UTF-8 友好（中文不转义）的 JSON 文本。"""
    try:
        data = model.model_dump(mode="json")
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:  # pragma: no cover - pydantic 以外对象兜底
        return json.dumps(model, ensure_ascii=False, indent=2, default=str)


def _write_model(path: Path, model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_json(model) + "\n", encoding="utf-8")


def _load_model(path: Path, model_cls):
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return model_cls(**data)
    except Exception as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
    return None


def _thumbnail_time(duration: float) -> float:
    if duration <= 0:
        return 0.0
    return max(0.0, min(10.0, duration * 0.1))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_pipeline(
    *,
    job_id: str,
    job_dir: str,
    source: dict,
    settings,
    progress: Optional[ProgressFn] = None,
) -> None:
    """顺序执行完整流水线，产物写入 ``job_dir``；失败抛 :class:`PipelineError`。"""
    job_path = Path(str(job_dir)).expanduser()
    try:
        job_path = job_path.resolve()
    except OSError:  # pragma: no cover
        pass
    job_path.mkdir(parents=True, exist_ok=True)
    source_data: Dict[str, Any] = dict(source or {})
    logger.info(
        "开始处理作业 %s（来源：%s）", job_id, source_data.get("source_type", "file")
    )

    # 1) 解析来源 -----------------------------------------------------------
    _emit(progress, "resolving", 0.02, "解析视频信息…")
    try:
        media = ingest.resolve_source(source_data, dest_dir=str(job_path), progress=progress)
    except ingest.IngestError as exc:
        raise PipelineError(str(exc)) from exc
    except Exception as exc:
        logger.exception("解析视频来源失败")
        raise PipelineError(f"解析视频来源失败：{exc}") from exc

    if not media.path or not Path(media.path).is_file():
        raise PipelineError("未能获取可用的视频文件，请重试。")
    if media.source_type not in _VALID_SOURCE_TYPES:  # 防御性修正
        fallback = str(source_data.get("source_type") or "file")
        media.source_type = fallback if fallback in _VALID_SOURCE_TYPES else "file"

    # 2) 媒体信息 + 缩略图 ---------------------------------------------------
    _emit(progress, "probing", 0.1, "读取媒体信息…")
    if not media.duration:
        try:
            probed = ingest.probe_media(media.path)
            media.duration = probed.duration
            if not media.payload:
                media.payload = dict(probed.payload or {})
        except ingest.IngestError as exc:
            logger.warning("读取媒体信息失败：%s", exc)

    thumbnail_file = job_path / "thumbnail.jpg"
    thumbnail_ok = bool(media.thumbnail_path) and Path(str(media.thumbnail_path)).is_file()
    if not thumbnail_ok:
        made = ingest.make_thumbnail(
            media.path, str(thumbnail_file), at=_thumbnail_time(media.duration)
        )
        media.thumbnail_path = str(thumbnail_file) if made else None
    _write_model(job_path / "media.json", media)
    _emit(
        progress,
        "probing",
        1.0,
        f"视频时长 {media.duration:.1f} 秒" if media.duration else "媒体信息读取完成",
    )

    # 3) 语音转文字（失败必须抛错） -----------------------------------------
    _emit(progress, "transcribing", 0.0, "准备语音转文字…")
    try:
        transcript = asr.transcribe(
            media.path, settings, duration=media.duration, progress=progress
        )
    except Exception as exc:
        logger.exception("语音转文字失败")
        raise PipelineError(f"语音转文字失败：{exc}") from exc
    _write_model(job_path / "transcript.json", transcript)
    if media.duration <= 0 and transcript.duration > 0:
        media.duration = transcript.duration
        _write_model(job_path / "media.json", media)

    # 4) 抽帧（失败降级） ---------------------------------------------------
    _emit(progress, "frames", 0.0, "抽取画面关键帧…")
    frames: List[Any] = []
    try:
        frames = vision.extract_frames(
            media.path,
            str(job_path / "frames"),
            max_frames=int(getattr(settings, "max_frames", 100) or 100),
            interval=float(getattr(settings, "frame_interval", 20.0) or 20.0),
            duration=media.duration,
            progress=progress,
        )
    except Exception as exc:
        logger.warning("抽帧失败（降级继续）：%s", exc)
    if not frames:
        _emit(progress, "frames", 1.0, "未抽取到关键帧（继续处理）")

    # 5) 画面文字 OCR（失败写空 ocr.json 继续） ------------------------------
    ocr = OcrResult()
    if frames:
        _emit(progress, "ocr", 0.0, "识别画面文字…")
        try:
            ocr = vision.run_ocr(frames, progress=progress)
        except Exception as exc:
            logger.warning("画面文字识别失败（降级为空结果）：%s", exc)
            ocr = OcrResult()
            _emit(progress, "ocr", 1.0, "画面文字识别失败（继续处理）")
    else:
        _emit(progress, "ocr", 1.0, "无关键帧，跳过画面文字识别")
    _write_model(job_path / "ocr.json", ocr)

    # 6) 可选视觉模型描述（默认关闭） ----------------------------------------
    visual_notes: List[str] = []
    try:
        visual_notes = vision.describe_frames(frames, settings, progress=progress)
    except Exception as exc:  # pragma: no cover - 可选能力
        logger.warning("视觉模型描述失败（忽略）：%s", exc)
        visual_notes = []

    # 7) DeepSeek 结构化总结（无 Key / 调用失败降级） ------------------------
    _emit(progress, "analyzing", 0.0, "生成内容总结…")
    summary: Summary = analyze.summarize(
        media,
        transcript,
        ocr,
        settings,
        visual_notes=visual_notes,
        progress=progress,
    )
    _write_model(job_path / "summary.json", summary)

    _emit(progress, "done", 1.0, "完成")
    logger.info(
        "作业 %s 处理完成：%s 句转写、%s 帧 OCR、总结模型=%s",
        job_id,
        len(transcript.segments),
        len(ocr.frames),
        summary.generated_by or "（降级）",
    )


def regenerate_summary(
    *,
    job_dir: str,
    settings,
    progress: Optional[ProgressFn] = None,
) -> Summary:
    """读取 job_dir 内已有产物，重新生成 ``summary.json`` 并返回。"""
    job_path = Path(str(job_dir)).expanduser()
    media = _load_model(job_path / "media.json", MediaInfo) or MediaInfo()
    transcript = _load_model(job_path / "transcript.json", Transcript) or Transcript()
    ocr = _load_model(job_path / "ocr.json", OcrResult) or OcrResult()

    if not media.title and transcript.text:
        media.title = media.title or "未命名视频"
    _emit(progress, "analyzing", 0.0, "重新生成内容总结…")
    summary = analyze.summarize(media, transcript, ocr, settings, progress=progress)
    _write_model(job_path / "summary.json", summary)
    _emit(progress, "done", 1.0, "总结已更新")
    return summary
