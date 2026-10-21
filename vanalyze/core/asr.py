"""本地语音转文字（faster-whisper / CTranslate2）。

要点：
- 模型实例进程内缓存（单例），避免每次作业重复加载；
- CUDA 优先，缺少 cuDNN/cuBLAS 或运行报错时自动降级 CPU（不崩溃）；
- VAD 过滤静音、按 ``segment.end / duration`` 上报进度；
- 输出契约中的 :class:`~vanalyze.models.Transcript`。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import ProgressFn, Transcript, TranscriptSegment
from . import gpu

logger = logging.getLogger(__name__)

#: 契约要求的中文 initial prompt
INITIAL_PROMPT = "以下是普通话视频的音频转写，请使用简体中文和规范标点。"

_CUDA_COMPUTE_TYPES = {
    "auto",
    "float16",
    "int8_float16",
    "int8",
    "bfloat16",
    "float32",
    "int8_float32",
}
_CPU_COMPUTE_TYPES = {"auto", "int8", "int8_float32", "float32"}

_MODEL_LOCK = threading.Lock()
#: key=(model_name, device, compute_type) -> WhisperModel
_MODEL_CACHE: Dict[Tuple[str, str, str], object] = {}
_MODEL_ORDER: List[Tuple[str, str, str]] = []


# ---------------------------------------------------------------------------
# 设备选择
# ---------------------------------------------------------------------------
def detect_device(settings) -> Tuple[str, str]:
    """返回 (device, compute_type)：有可用 CUDA → ("cuda","float16")，否则 ("cpu","int8")。

    该函数必须足够快（健康检查会调用），不会加载 Whisper 模型。
    """
    requested = (getattr(settings, "whisper_device", "auto") or "auto").strip().lower()
    compute = (getattr(settings, "whisper_compute_type", "auto") or "auto").strip().lower()
    if compute not in _CUDA_COMPUTE_TYPES | _CPU_COMPUTE_TYPES:
        compute = "auto"

    if requested == "cuda":
        return "cuda", (compute if compute != "auto" else "float16")
    if requested == "cpu":
        cpu_compute = compute if compute in _CPU_COMPUTE_TYPES and compute != "auto" else "int8"
        return "cpu", cpu_compute

    # auto
    if gpu.cuda_available():
        return "cuda", (compute if compute not in ("auto",) else "float16")
    return "cpu", (compute if compute in _CPU_COMPUTE_TYPES and compute != "auto" else "int8")


def _safe_progress(progress: Optional[ProgressFn], stage: str, pct: float, message: Optional[str]) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0.0, min(1.0, float(pct))), message)
    except Exception:  # pragma: no cover - 回调异常不能影响转写
        logger.debug("progress 回调异常", exc_info=True)


# ---------------------------------------------------------------------------
# 模型加载（缓存 + 自动降级）
# ---------------------------------------------------------------------------
def _create_model(model_name: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # pragma: no cover - 依赖缺失
        raise RuntimeError(
            f"faster-whisper 未正确安装，无法进行语音识别：{exc}"
        ) from exc
    try:
        return WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        text = str(exc).lower()
        download_hint = any(
            k in text
            for k in (
                "huggingface",
                "connection",
                "couldn't connect",
                "timed out",
                "timeout",
                "offline",
                "download",
                "401",
                "403",
                "404",
                "repository",
                "proxy",
            )
        )
        if download_hint and (device == "cpu" or "local entry not found" in text):
            raise RuntimeError(
                f"Whisper 模型「{model_name}」加载失败：{exc}。"
                "若无法访问 HuggingFace，可设置环境变量 HF_ENDPOINT=https://hf-mirror.com 后重试。"
            ) from exc
        raise


def _get_model(settings) -> Tuple[object, str, str]:
    """加载/复用 Whisper 模型；CUDA 失败自动降级 CPU。"""
    model_name = str(getattr(settings, "whisper_model", "large-v3") or "large-v3").strip()
    device, compute_type = detect_device(settings)
    key = (model_name, device, compute_type)

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached, device, compute_type

    logger.info("加载 Whisper 模型：%s device=%s compute=%s", model_name, device, compute_type)
    try:
        model = _create_model(model_name, device, compute_type)
    except Exception as exc:
        if device != "cuda":
            raise
        logger.warning("CUDA 初始化失败，自动降级 CPU 推理：%s", exc)
        device, compute_type = "cpu", "int8"
        key = (model_name, device, compute_type)
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached, device, compute_type
        model = _create_model(model_name, device, compute_type)

    with _MODEL_LOCK:
        _MODEL_CACHE[key] = model
        _MODEL_ORDER.append(key)
        # 最多保留 2 个模型实例（切换模型/设备时释放旧实例，避免显存堆积）
        while len(_MODEL_ORDER) > 2:
            old = _MODEL_ORDER.pop(0)
            if old != key:
                _MODEL_CACHE.pop(old, None)
    return model, device, compute_type


# ---------------------------------------------------------------------------
# 媒体辅助
# ---------------------------------------------------------------------------
def _probe_container(path: str) -> Tuple[bool, float]:
    """返回 (是否有音频轨, 容器时长秒)；无法解码时抛 RuntimeError。"""
    try:
        import av
    except Exception:  # pragma: no cover - av 一般随 faster-whisper 安装
        return True, 0.0
    try:
        with av.open(path) as container:
            has_audio = any(s.type == "audio" for s in container.streams)
            duration = 0.0
            if container.duration:
                duration = float(container.duration) / float(av.time_base)
            else:
                for s in container.streams:
                    if s.duration and s.time_base:
                        duration = max(duration, float(s.duration * s.time_base))
            return has_audio, duration
    except Exception as exc:
        raise RuntimeError(f"无法读取媒体文件：{exc}") from exc


def _audio_has_signal(path: str, max_seconds: float = 60.0) -> bool:
    """粗略判断音频是否有能量（用于区分「静音视频」和「VAD 误杀音乐/演唱」）。"""
    try:
        import av
        import numpy as np
    except Exception:  # pragma: no cover
        return False
    try:
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                return False
            sample_rate = int(getattr(stream.codec_context, "sample_rate", 0) or 0)
            limit = int(max_seconds * sample_rate) if sample_rate > 0 else 0
            total_abs = 0.0
            sample_count = 0
            peak = 0.0
            for frame in container.decode(stream):
                try:
                    array = frame.to_ndarray()
                except Exception:  # pragma: no cover
                    continue
                if array.size:
                    absolute = np.abs(array)
                    total_abs += float(absolute.sum())
                    peak = max(peak, float(absolute.max()))
                    sample_count += int(array.size)
                if limit and sample_count >= limit:
                    break
            if sample_count == 0:
                return False
            mean_abs = total_abs / float(sample_count)
            return mean_abs > 0.002 or peak > 0.05
    except Exception as exc:  # pragma: no cover
        logger.debug("音频能量检测失败 %s：%s", path, exc)
        return False


def _looks_like_cuda_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        k in text
        for k in (
            "cuda",
            "cudnn",
            "cublas",
            "gpu",
            "out of memory",
            "cuda_error",
            "no kernel image",
            "device",
        )
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def transcribe(
    media_path: str,
    settings,
    *,
    duration: float = 0.0,
    progress: Optional[ProgressFn] = None,
) -> Transcript:
    """把媒体文件转写为 :class:`Transcript`。

    - 空音轨/静音：返回空 segments，不抛错；
    - 文件不可读 / 模型加载失败：抛 RuntimeError（pipeline 会包装为 PipelineError）。
    """
    path = Path(str(media_path))
    if not path.is_file():
        raise RuntimeError(f"找不到音视频文件：{path}")

    _safe_progress(progress, "transcribing", 0.01, "读取媒体信息…")
    has_audio, probed_duration = _probe_container(str(path))
    total_duration = float(duration or probed_duration or 0.0)
    language = (getattr(settings, "whisper_language", "auto") or "auto").strip()
    whisper_language = None if language.lower() in ("", "auto", "none") else language

    if not has_audio:
        logger.info("媒体文件没有音频轨，返回空转写：%s", path.name)
        _safe_progress(progress, "transcribing", 1.0, "该视频没有音频轨，跳过转写")
        return Transcript(language=whisper_language or "zh", duration=total_duration, segments=[], text="")

    _safe_progress(progress, "transcribing", 0.02, "加载语音识别模型…")
    model, device, compute_type = _get_model(settings)
    logger.info("开始转写：%s（device=%s, compute=%s）", path.name, device, compute_type)
    _safe_progress(progress, "transcribing", 0.03, f"语音转文字（{device.upper()}）…")

    kwargs = dict(
        language=whisper_language,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
        initial_prompt=INITIAL_PROMPT,
    )

    try:
        active_model = model
        segments, info = _transcribe_once(active_model, str(path), kwargs, total_duration, progress)
    except Exception as exc:
        if device != "cuda":
            raise RuntimeError(f"语音转文字失败：{exc}") from exc
        logger.warning("CUDA 转写失败（%s），自动降级 CPU 重试", exc)
        _safe_progress(progress, "transcribing", 0.05, "GPU 推理失败，正在切换 CPU…")
        try:
            model2, device, compute_type = _get_model(
                settings.model_copy(update={"whisper_device": "cpu", "whisper_compute_type": "int8"})
                if hasattr(settings, "model_copy")
                else settings
            )
            active_model = model2
            segments, info = _transcribe_once(active_model, str(path), kwargs, total_duration, progress)
        except Exception as exc2:
            raise RuntimeError(f"语音转文字失败：{exc2}") from exc2

    # VAD 兜底：音乐 / 演唱类视频可能被 VAD 全部过滤（segments 为空），
    # 若音频确实有能量，则关闭 VAD 重试一次，避免整段内容丢失；纯静音不重试。
    # 注意：重试时去掉中文 initial_prompt —— 中文提示词作用于英文歌曲会引发
    # Whisper 复读/幻觉（实测 initial_prompt 会导致输出与音频无关的内容）。
    if not segments and total_duration > 1.0 and _audio_has_signal(str(path)):
        logger.warning("VAD 过滤掉了全部音频（可能是音乐/演唱内容），关闭 VAD 重新转写")
        _safe_progress(progress, "transcribing", 0.4, "检测到音乐/演唱内容，关闭静音过滤重新转写…")
        no_vad_kwargs = dict(kwargs)
        no_vad_kwargs["vad_filter"] = False
        no_vad_kwargs["initial_prompt"] = None
        try:
            segments, info = _transcribe_once(
                active_model, str(path), no_vad_kwargs, total_duration, progress
            )
        except Exception as exc:  # pragma: no cover - 兜底失败则保持空转写
            logger.warning("关闭 VAD 重试失败：%s", exc)

    total_duration = float(total_duration or getattr(info, "duration", 0.0) or 0.0)
    if total_duration <= 0 and segments:
        total_duration = float(segments[-1].end)

    transcript_segments: List[TranscriptSegment] = [
        TranscriptSegment(id=i + 1, start=float(seg.start), end=float(seg.end), text=seg.text)
        for i, seg in enumerate(segments)
        if (seg.text or "").strip()
    ]
    text = "\n".join(s.text for s in transcript_segments)
    detected_language = getattr(info, "language", None) or whisper_language or "zh"

    _safe_progress(progress, "transcribing", 1.0, f"转写完成，共 {len(transcript_segments)} 句")
    logger.info("转写完成：%s 句，语言 %s", len(transcript_segments), detected_language)
    return Transcript(
        language=str(detected_language),
        duration=total_duration,
        segments=transcript_segments,
        text=text,
    )


def _transcribe_once(model, path: str, kwargs: dict, total_duration: float, progress: Optional[ProgressFn]):
    """调用一次模型转写，返回 (segments 列表, info)。"""
    raw_segments, info = model.transcribe(path, **kwargs)
    duration = float(total_duration or getattr(info, "duration", 0.0) or 0.0)
    collected = []
    last_reported = -1.0
    for index, segment in enumerate(raw_segments):
        collected.append(segment)
        if duration > 0:
            ratio = min(1.0, max(0.0, float(segment.end) / duration))
        else:
            ratio = 0.0
        if index % 20 == 0 or (ratio - last_reported) >= 0.01:
            last_reported = ratio
            _safe_progress(
                progress,
                "transcribing",
                0.03 + ratio * 0.96,
                f"语音转文字 {ratio * 100:.0f}%",
            )
    return collected, info
