"""媒体读写工具（PyAV 优先），统一规避 Windows 中文路径问题。

- 视频解码 / 抽帧：``PyAV``（``av``），对 UTF-8 路径支持良好；
- 读图：``cv2.imdecode(np.fromfile(...))``；
- 写 JPEG：``cv2.imencode`` + ``Path.write_bytes``（或 PIL）。
- ffmpeg 可执行文件：``imageio_ffmpeg.get_ffmpeg_exe()``（不重复下载）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_FFMPEG_EXE: Optional[str] = None
_FFMPEG_CHECKED = False


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
def ffmpeg_exe() -> Optional[str]:
    """返回 imageio-ffmpeg 自带的 ffmpeg 路径；不可用时返回 None。"""
    global _FFMPEG_EXE, _FFMPEG_CHECKED
    if _FFMPEG_CHECKED:
        return _FFMPEG_EXE
    _FFMPEG_CHECKED = True
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            _FFMPEG_EXE = str(exe)
        else:
            logger.warning("未找到可用的 ffmpeg（imageio-ffmpeg）")
    except Exception as exc:  # pragma: no cover
        logger.warning("加载 imageio-ffmpeg 失败：%s", exc)
    return _FFMPEG_EXE


# ---------------------------------------------------------------------------
# 图像读写
# ---------------------------------------------------------------------------
def resize_width(bgr: np.ndarray, max_width: int) -> np.ndarray:
    """等比缩放到宽度不超过 max_width（不放大）。"""
    if bgr is None or bgr.size == 0 or max_width <= 0:
        return bgr
    height, width = bgr.shape[:2]
    if width <= max_width:
        return bgr
    import cv2

    scale = float(max_width) / float(width)
    return cv2.resize(
        bgr,
        (int(max_width), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def write_jpeg(path: str | Path, bgr: np.ndarray, quality: int = 82) -> bool:
    """把 BGR 数组写成 JPEG（UTF-8 路径安全）。"""
    try:
        import cv2

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return False
        target.write_bytes(buffer.tobytes())
        return True
    except Exception as exc:
        logger.debug("写图片失败 %s：%s", path, exc)
        return False


def read_image_bgr(path: str | Path) -> Optional[np.ndarray]:
    """读取图片为 BGR 数组（UTF-8 路径安全）。"""
    try:
        import cv2

        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception as exc:
        logger.debug("读图片失败 %s：%s", path, exc)
        return None


# ---------------------------------------------------------------------------
# 视频元信息与抽帧（PyAV）
# ---------------------------------------------------------------------------
def probe_video(path: str | Path) -> dict:
    """返回视频元信息字典；失败时尽量返回部分信息，不抛异常。

    键：duration / width / height / fps / has_video / has_audio / codec / audio_codec
    """
    info: dict = {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "has_video": False,
        "has_audio": False,
        "codec": None,
        "audio_codec": None,
    }
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration:
                info["duration"] = float(container.duration) / float(av.time_base)
            for stream in container.streams:
                if stream.type == "video" and not info["has_video"]:
                    info["has_video"] = True
                    info["codec"] = stream.codec_context.name
                    width = getattr(stream.codec_context, "width", 0) or 0
                    height = getattr(stream.codec_context, "height", 0) or 0
                    info["width"] = int(width)
                    info["height"] = int(height)
                    rate = getattr(stream, "average_rate", None)
                    if rate:
                        info["fps"] = float(rate)
                elif stream.type == "audio":
                    info["has_audio"] = True
                    info["audio_codec"] = stream.codec_context.name
            if info["duration"] <= 0:
                for stream in container.streams:
                    if stream.duration and stream.time_base:
                        try:
                            info["duration"] = max(
                                info["duration"], float(stream.duration * stream.time_base)
                            )
                        except Exception:  # pragma: no cover
                            pass
    except Exception as exc:
        logger.debug("PyAV 读取媒体信息失败 %s：%s", path, exc)

    if info["duration"] <= 0 or not info["has_video"]:
        # 兜底：OpenCV（对部分容器/编码更宽容）
        try:
            import cv2

            cap = cv2.VideoCapture(str(path))
            if cap.isOpened():
                info["has_video"] = True
                fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                if info["duration"] <= 0 and fps > 0 and frames > 0:
                    info["duration"] = float(frames) / float(fps)
                if not info["fps"]:
                    info["fps"] = float(fps) if fps else 0.0
                if not info["width"]:
                    info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                if not info["height"]:
                    info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cap.release()
        except Exception:  # pragma: no cover
            pass
    return info


class VideoReader:
    """基于 PyAV 的视频读取器：按时间戳取帧。

    用法::

        with VideoReader(path) as reader:
            frame = reader.frame_at(12.5)   # BGR ndarray 或 None

    说明：seek 到目标时间戳之前的关键帧再顺序解码到目标帧，兼顾速度与准确性。
    """

    def __init__(self, path: str | Path):
        import av

        self.path = str(path)
        self._container = None
        self._stream = None
        self._av = av
        self.duration = 0.0
        self.fps = 0.0
        self.width = 0
        self.height = 0
        self.frame_count = 0
        self._time_base = 1.0
        self._open()

    def _open(self) -> None:
        av = self._av
        container = av.open(self.path)
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            container.close()
            raise RuntimeError("媒体文件没有视频轨")
        self._container = container
        self._stream = stream
        if container.duration:
            self.duration = float(container.duration) / float(av.time_base)
        self._time_base = float(stream.time_base) if stream.time_base else 1.0
        if self.duration <= 0 and stream.duration:
            self.duration = float(stream.duration * self._time_base)
        rate = getattr(stream, "average_rate", None)
        if rate:
            self.fps = float(rate)
        self.width = int(getattr(stream.codec_context, "width", 0) or 0)
        self.height = int(getattr(stream.codec_context, "height", 0) or 0)
        if self.fps > 0 and self.duration > 0:
            self.frame_count = int(self.duration * self.fps)

    # -- 上下文管理 ---------------------------------------------------
    def __enter__(self) -> "VideoReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        container = self._container
        self._container = None
        self._stream = None
        if container is not None:
            try:
                container.close()
            except Exception:  # pragma: no cover
                pass

    # -- 取帧 ---------------------------------------------------------
    def _decode_frame_at(self, at: float) -> Optional[np.ndarray]:
        container, stream = self._container, self._stream
        if container is None or stream is None:
            return None
        target = max(0.0, float(at))
        last_frame = None
        try:
            # 注意：即使 target=0 也要 seek 回起点，否则会从上次解码位置继续读
            offset = int(target / self._time_base)
            try:
                container.seek(offset, stream=stream, backward=True)
            except Exception:  # pragma: no cover - 少数容器不支持 seek
                try:
                    container.seek(0)
                except Exception:
                    pass
            for frame in container.decode(stream):
                last_frame = frame
                frame_time = frame.time
                if frame_time is None or float(frame_time) + 1e-6 >= target:
                    return frame.to_ndarray(format="bgr24")
        except Exception as exc:
            logger.debug("PyAV 取帧失败 %s @ %.2fs：%s", self.path, target, exc)
        if last_frame is not None:
            try:
                return last_frame.to_ndarray(format="bgr24")
            except Exception:  # pragma: no cover
                return None
        return None

    def frame_at(self, at: float) -> Optional[np.ndarray]:
        """取 at 秒处的一帧（BGR ndarray）；失败返回 None。"""
        return self._decode_frame_at(at)

    def first_frame(self) -> Optional[np.ndarray]:
        return self._decode_frame_at(0.0)


def grab_frame(path: str | Path, at: float = 0.0) -> Optional[np.ndarray]:
    """独立打开视频并取一帧（BGR ndarray）；失败返回 None。"""
    try:
        with VideoReader(path) as reader:
            return reader.frame_at(at)
    except Exception as exc:
        logger.debug("取帧失败 %s：%s", path, exc)
        return None
