"""视频来源解析与采集：本地文件 / 网页链接（yt-dlp）/ 浏览器标签页。

- 本地文件：不复制，直接引用原路径（``local_input=True``），并在作业目录生成缩略图；
- 网页链接：yt-dlp Python API 下载 ``bestvideo*+bestaudio/best``，ffmpeg 用
  ``imageio_ffmpeg.get_ffmpeg_exe()``，进度钩子换算为 0..1；
- tab 来源：页面链接优先走 yt-dlp；``video_url`` 为 http(s) 直链时兜底流式下载；
  为 ``blob:`` 时给出中文指引错误。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from ..models import MediaInfo, ProgressFn
from . import _media

logger = logging.getLogger(__name__)

#: blob / DRM 场景的中文指引（与契约 §6.3 保持一致）
BLOB_HINT = (
    "无法解析该网页的视频。该页面可能是 blob/DRM 受保护的流媒体，"
    "请改用『下载视频后拖入网页』或换用视频页面链接。"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_VIDEO_EXTS = {
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v", ".ts",
    ".mpg", ".mpeg", ".wmv", ".3gp", ".ogv", ".rmvb",
}
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma"}
_PLAYLIST_EXTS = {".m3u8", ".mpd"}

_MAX_THUMB_WIDTH = 960
_THUMB_AT_MAX = 10.0

_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=60.0, write=60.0, pool=30.0)


class IngestError(RuntimeError):
    """来源解析/下载失败（消息为可直接展示的中文）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _emit(progress: Optional[ProgressFn], stage: str, pct: float, message: Optional[str]) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0.0, min(1.0, float(pct))), message)
    except Exception:  # pragma: no cover - 回调异常不影响下载
        logger.debug("progress 回调异常", exc_info=True)


def _url_path_suffix(url: str) -> str:
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    return Path(path).suffix.lower()


def _is_http(url: str) -> bool:
    return bool(url) and str(url).lower().startswith(("http://", "https://"))


def _is_direct_media_url(url: str) -> bool:
    return _url_path_suffix(url) in (_VIDEO_EXTS | _AUDIO_EXTS)


def _is_playlist_url(url: str) -> bool:
    return _url_path_suffix(url) in _PLAYLIST_EXTS


def _find_downloaded(dest: Path) -> Optional[Path]:
    """在作业目录里找 yt-dlp 下载的 ``source.*`` 媒体文件。"""
    candidates: List[Path] = []
    try:
        for p in dest.glob("source.*"):
            if not p.is_file():
                continue
            suffix = p.suffix.lower()
            if suffix in {".part", ".ytdl", ".temp", ".download", ".jpg", ".jpeg", ".png", ".webp", ".json"}:
                continue
            try:
                if p.stat().st_size > 0:
                    candidates.append(p)
            except OSError:
                continue
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _info_filepath(info: Dict[str, Any]) -> Optional[Path]:
    for entry in info.get("requested_downloads") or []:
        for key in ("filepath", "filename"):
            value = entry.get(key)
            if value and Path(value).is_file():
                return Path(value)
    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value and Path(value).is_file():
            return Path(value)
    return None


def _pick_thumbnail_url(info: Dict[str, Any]) -> Optional[str]:
    url = info.get("thumbnail")
    if isinstance(url, str) and _is_http(url):
        return url
    thumbs = info.get("thumbnails") or []
    if isinstance(thumbs, list):
        for thumb in reversed(thumbs):
            if isinstance(thumb, dict):
                candidate = thumb.get("url")
                if isinstance(candidate, str) and _is_http(candidate):
                    return candidate
    return None


# ---------------------------------------------------------------------------
# 进度钩子
# ---------------------------------------------------------------------------
def _make_progress_hook(progress: Optional[ProgressFn]):
    def hook(data: Dict[str, Any]) -> None:
        try:
            status = data.get("status")
            if status == "downloading":
                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                    or data.get("total")
                    or 0
                )
                done = data.get("downloaded_bytes") or 0
                if total:
                    pct = min(0.99, max(0.0, float(done) / float(total)))
                else:
                    pct = min(0.95, 0.05 + float(done) / (100.0 * 1024 * 1024)) if done else 0.02
                _emit(progress, "downloading", pct, f"下载视频 {pct * 100:.0f}%")
            elif status == "finished":
                _emit(progress, "downloading", 0.99, "下载完成，正在合并音视频…")
        except Exception:  # pragma: no cover
            logger.debug("下载进度回调异常", exc_info=True)

    return hook


# ---------------------------------------------------------------------------
# yt-dlp 下载
# ---------------------------------------------------------------------------
def _ytdlp_options(dest: Path, progress: Optional[ProgressFn], *, fmt: str) -> Dict[str, Any]:
    ffmpeg = _media.ffmpeg_exe()
    opts: Dict[str, Any] = {
        "outtmpl": str(dest / "source.%(ext)s"),
        "format": fmt,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "overwrites": True,
        "socket_timeout": 30,
        "retries": 1,
        "fragment_retries": 1,
        "progress_hooks": [_make_progress_hook(progress)],
        "http_headers": {"User-Agent": _UA},
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        opts["merge_output_format"] = "mp4/mkv"
    return opts


def _friendly_ytdlp_error(url: str, exc: Optional[BaseException]) -> str:
    detail = str(exc or "").strip()
    low = detail.lower()
    if "unsupported url" in low:
        return (
            f"无法解析该链接（{url}）。请确认它是视频页面链接，"
            "或改用『下载视频后拖入网页』。"
        )
    if any(k in low for k in ("login", "sign in", "cookies", "private", "members-only", "会员")):
        return "该视频需要登录或权限（可能是会员/私密视频），无法直接下载。"
    if "drm" in low:
        return BLOB_HINT
    if any(k in low for k in ("timed out", "timeout")):
        return "下载视频超时（网络较慢或站点限制），请稍后重试。"
    if any(k in low for k in ("ssl", "certificate")):
        return "下载视频失败：SSL 证书校验错误，请检查网络代理设置。"
    if "ffmpeg" in low:
        return f"下载视频失败：合并音视频时 ffmpeg 出错（{detail[:160]}）。"
    if detail:
        return f"下载视频失败：{detail[:300]}"
    return "下载视频失败：未知错误，请稍后重试。"


def _ytdlp_download(
    url: str,
    dest: Path,
    progress: Optional[ProgressFn],
    *,
    single_file: bool = False,
) -> Dict[str, Any]:
    """用 yt-dlp 下载网页视频，返回 info dict（含标题/时长/上传者/封面）。"""
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:  # pragma: no cover - 依赖缺失
        raise IngestError(f"未安装 yt-dlp，无法下载网页视频：{exc}") from exc

    ffmpeg = _media.ffmpeg_exe()
    if single_file or not ffmpeg:
        formats = ["best"]
        if ffmpeg:
            formats.append("bestvideo*+bestaudio/best")
        else:
            logger.warning("未检测到 ffmpeg，网页视频将降级为单文件流 best")
    else:
        # 依次尝试：合并流 → 通用合并选择器 → 单文件流（YouTube 偶发“格式不可用”时兜底）
        formats = ["bestvideo*+bestaudio/best", "bv*+ba/b", "best"]

    last_exc: Optional[BaseException] = None
    for index, fmt in enumerate(formats):
        _emit(
            progress,
            "downloading",
            0.01 if index == 0 else 0.02,
            "开始下载视频…" if index == 0 else "尝试备用下载方式…",
        )
        try:
            opts = _ytdlp_options(dest, progress, fmt=fmt)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if isinstance(info, dict):
                if info.get("entries"):
                    entries = [e for e in info.get("entries") or [] if isinstance(e, dict)]
                    if entries:
                        info = entries[0]
                return info
        except Exception as exc:  # yt-dlp 抛出 DownloadError 等
            last_exc = exc
            logger.warning("yt-dlp 下载失败（format=%s）：%s", fmt, exc)
    raise IngestError(_friendly_ytdlp_error(url, last_exc))


# ---------------------------------------------------------------------------
# HTTP 直链下载
# ---------------------------------------------------------------------------
_CTYPE_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "video/x-flv": ".flv",
    "video/x-msvideo": ".avi",
    "video/mp2t": ".ts",
    "video/ogg": ".ogv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "application/octet-stream": ".mp4",
}


def _direct_download(
    url: str,
    dest: Path,
    progress: Optional[ProgressFn],
    *,
    page_url: Optional[str] = None,
) -> Path:
    """流式下载 http(s) 直链到 ``dest/source.<ext>``，带百分比进度。"""
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if page_url and _is_http(page_url):
        headers["Referer"] = page_url
    tmp = dest / "source.download"
    emit_done = 0
    emit_last = -1.0
    try:
        with httpx.Client(follow_redirects=True, timeout=_HTTP_TIMEOUT, headers=headers) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise IngestError(
                        f"下载视频失败（HTTP {response.status_code}），该链接可能需要登录或已失效。"
                    )
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if "mpegurl" in content_type or "dash+xml" in content_type:
                    raise IngestError("该地址是流媒体播放列表（m3u8/mpd），需要交给 yt-dlp 解析。")
                total = int(response.headers.get("content-length") or 0)
                suffix = _url_path_suffix(url)
                if suffix in _PLAYLIST_EXTS:
                    raise IngestError("该地址是流媒体播放列表（m3u8/mpd），需要交给 yt-dlp 解析。")
                if suffix not in (_VIDEO_EXTS | _AUDIO_EXTS):
                    suffix = _CTYPE_EXT.get(content_type, ".mp4")
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes(256 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        emit_done += len(chunk)
                        if total:
                            pct = min(0.99, float(emit_done) / float(total))
                        else:
                            pct = min(0.95, 0.05 + emit_done / (100.0 * 1024 * 1024))
                        if pct - emit_last >= 0.01:
                            emit_last = pct
                            _emit(progress, "downloading", pct, f"下载视频 {pct * 100:.0f}%")
        if emit_done <= 0:
            raise IngestError("下载视频失败：服务器返回了空内容。")
        final = dest / f"source{suffix}"
        if final.exists():
            final.unlink()
        tmp.replace(final)
        _emit(progress, "downloading", 1.0, "下载完成")
        return final
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------
def _download_thumbnail(url: str, out_path: Path) -> bool:
    """下载网络封面并保存为 JPEG；成功返回 True。"""
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0)) as client:
            response = client.get(url, headers={"User-Agent": _UA})
        if response.status_code >= 400 or not response.content:
            return False
        data = response.content
        if len(data) < 1024:
            return False
        try:
            from io import BytesIO

            from PIL import Image

            image = Image.open(BytesIO(data)).convert("RGB")
            if image.width > _MAX_THUMB_WIDTH:
                ratio = _MAX_THUMB_WIDTH / float(image.width)
                image = image.resize((_MAX_THUMB_WIDTH, max(1, int(image.height * ratio))))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(out_path, "JPEG", quality=82)
            return True
        except Exception:
            content_type = (response.headers.get("content-type") or "").lower()
            if "image" in content_type:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(data)
                return True
            return False
    except Exception as exc:  # pragma: no cover - 网络失败不阻断
        logger.debug("下载封面失败 %s：%s", url, exc)
        return False


def _thumbnail_time(duration: float) -> float:
    if duration <= 0:
        return 0.0
    return max(0.0, min(_THUMB_AT_MAX, duration * 0.1))


def _ensure_thumbnail(
    media_path: str,
    dest: Path,
    info: Optional[Dict[str, Any]] = None,
    *,
    poster: Optional[str] = None,
    duration: float = 0.0,
) -> Optional[str]:
    """生成 ``dest/thumbnail.jpg``：优先网络封面，其次视频帧。"""
    out = dest / "thumbnail.jpg"
    candidates: List[str] = []
    if info:
        thumb = _pick_thumbnail_url(info)
        if thumb:
            candidates.append(thumb)
    if isinstance(poster, str) and _is_http(poster):
        candidates.append(poster)
    for url in candidates:
        if _download_thumbnail(url, out):
            return str(out)
    try:
        frame = _media.grab_frame(media_path, _thumbnail_time(duration))
        if frame is None:
            frame = _media.grab_frame(media_path, 0.0)
        if frame is not None:
            frame = _media.resize_width(frame, _MAX_THUMB_WIDTH)
            if _media.write_jpeg(out, frame, quality=82):
                return str(out)
    except Exception as exc:  # pragma: no cover - 缩略图失败可缺失
        logger.debug("生成缩略图失败：%s", exc)
    return None


# ---------------------------------------------------------------------------
# 媒体探测
# ---------------------------------------------------------------------------
def probe_media(path: str) -> MediaInfo:
    """读取媒体文件的基本信息（至少填 path/duration/title/ext）。"""
    target = Path(str(path)).expanduser()
    try:
        target = target.resolve()
    except OSError:  # pragma: no cover
        pass
    if not target.is_file():
        raise IngestError(f"文件不存在或无法读取：{target}")
    info = _media.probe_video(target)
    duration = float(info.get("duration") or 0.0)
    has_video = bool(info.get("has_video"))
    has_audio = bool(info.get("has_audio"))
    if duration <= 0 and not has_video and not has_audio:
        raise IngestError(f"无法识别该文件为可播放的视频：{target.name}")
    ext = target.suffix.lower().lstrip(".")
    payload: Dict[str, Any] = {
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": round(float(info.get("fps") or 0.0), 3),
        "has_video": has_video,
        "has_audio": has_audio,
        "video_codec": info.get("codec"),
        "audio_codec": info.get("audio_codec"),
    }
    return MediaInfo(
        path=str(target),
        title=target.stem,
        duration=duration,
        ext=ext or None,
        payload=payload,
    )


def make_thumbnail(media_path: str, out_path: str, at: Optional[float] = None) -> bool:
    """从视频抽取一帧存为 JPEG，成功返回 True（失败不抛错）。"""
    try:
        source = Path(str(media_path))
        if not source.is_file():
            return False
        if at is None:
            duration = float(_media.probe_video(source).get("duration") or 0.0)
            at = _thumbnail_time(duration)
        frame = _media.grab_frame(source, max(0.0, float(at)))
        if frame is None:
            frame = _media.grab_frame(source, 0.0)
        if frame is None:
            return False
        frame = _media.resize_width(frame, _MAX_THUMB_WIDTH)
        return _media.write_jpeg(out_path, frame, quality=82)
    except Exception as exc:  # pragma: no cover
        logger.warning("生成缩略图失败 %s：%s", media_path, exc)
        return False


# ---------------------------------------------------------------------------
# 来源解析
# ---------------------------------------------------------------------------
def _resolve_file(source: Dict[str, Any], dest: Path, progress: Optional[ProgressFn]) -> MediaInfo:
    raw = source.get("file_path") or source.get("path") or source.get("url")
    if not raw:
        raise IngestError("没有提供本地视频文件路径。")
    target = Path(str(raw)).expanduser()
    try:
        target = target.resolve()
    except OSError:  # pragma: no cover
        pass
    if not target.exists():
        raise IngestError(f"文件不存在：{target}")
    if not target.is_file():
        raise IngestError(f"不是有效的视频文件：{target}")
    if not os.access(target, os.R_OK):
        raise IngestError(f"文件无法读取（权限不足）：{target}")
    suffix = target.suffix.lower()
    if suffix not in (_VIDEO_EXTS | _AUDIO_EXTS):
        # 扩展名未知时尝试探测；确实无法解码才报错
        try:
            media = probe_media(str(target))
        except IngestError:
            raise IngestError(
                f"不支持该文件格式（{suffix or '无扩展名'}）：{target.name}。"
                "请选择 mp4/mkv/webm/mov 等常见视频文件。"
            ) from None
    else:
        media = probe_media(str(target))

    media.source_type = "file"
    media.local_input = True
    media.source_url = None
    media.webpage_url = None
    provided_title = str(source.get("title") or "").strip()
    if provided_title:
        media.title = provided_title
    elif not media.title:
        media.title = target.stem
    thumbnail = _ensure_thumbnail(str(target), dest, duration=media.duration)
    media.thumbnail_path = thumbnail
    return media


def _build_media_info(
    media_path: Path,
    *,
    source_type: str,
    source_url: Optional[str],
    page_url: Optional[str],
    title: str = "",
    provided_duration: float = 0.0,
    info: Optional[Dict[str, Any]] = None,
    poster: Optional[str] = None,
    dest: Path,
) -> MediaInfo:
    media = probe_media(str(media_path))
    dynamic = info or {}
    media.source_type = source_type  # type: ignore[assignment]
    media.local_input = False
    media.source_url = source_url or None
    media.webpage_url = page_url or dynamic.get("webpage_url") or source_url or None
    title = (title or dynamic.get("title") or "").strip()
    if title:
        media.title = title
    uploader = dynamic.get("uploader") or dynamic.get("channel") or dynamic.get("creator")
    if uploader:
        media.uploader = str(uploader)
    description = dynamic.get("description")
    if description:
        media.description = str(description)[:2000]
    if media.duration <= 0 and provided_duration > 0:
        media.duration = float(provided_duration)
    extractor = dynamic.get("extractor_key") or dynamic.get("extractor")
    if extractor:
        media.payload["extractor"] = str(extractor)
    if dynamic.get("id"):
        media.payload["source_id"] = str(dynamic.get("id"))
    thumb_url = _pick_thumbnail_url(dynamic)
    if thumb_url:
        media.payload["thumbnail_url"] = thumb_url
    media.thumbnail_path = _ensure_thumbnail(
        str(media_path), dest, info, poster=poster, duration=media.duration
    )
    return media


def _resolve_url(source: Dict[str, Any], dest: Path, progress: Optional[ProgressFn]) -> MediaInfo:
    url = str(source.get("url") or "").strip()
    if not url:
        raise IngestError("没有提供网页链接。")
    if not _is_http(url):
        raise IngestError("网页链接必须以 http:// 或 https:// 开头。")

    _emit(progress, "downloading", 0.0, "正在解析网页视频…")
    info: Optional[Dict[str, Any]] = None
    media_path: Optional[Path] = None
    try:
        info = _ytdlp_download(url, dest, progress)
        media_path = _info_filepath(info) or _find_downloaded(dest)
    except IngestError as exc:
        logger.warning("yt-dlp 解析失败：%s", exc)
        last_error: Optional[BaseException] = exc
    else:
        last_error = None

    if media_path is None and _is_direct_media_url(url):
        try:
            media_path = _direct_download(url, dest, progress)
        except IngestError as exc:
            logger.warning("直链下载失败：%s", exc)
            last_error = last_error or exc

    if media_path is None:
        raise IngestError(
            "无法解析该网页的视频。"
            + (f"（{str(last_error)[:200]}）" if last_error else "")
            + "该页面可能是 blob/DRM 受保护的流媒体，"
            "请改用『下载视频后拖入网页』或换用视频页面链接。"
        )
    _emit(progress, "downloading", 1.0, "下载完成")
    return _build_media_info(
        media_path,
        source_type="url",
        source_url=url,
        page_url=str(source.get("page_url") or url),
        title=str(source.get("title") or ""),
        info=info,
        dest=dest,
    )


def _resolve_tab(source: Dict[str, Any], dest: Path, progress: Optional[ProgressFn]) -> MediaInfo:
    page_url = str(source.get("page_url") or "").strip()
    video_url = str(source.get("video_url") or "").strip()
    title = str(source.get("title") or "").strip()
    provided_duration = float(source.get("duration") or 0.0)
    poster = source.get("poster")
    poster = poster if isinstance(poster, str) else None

    errors: List[str] = []
    _emit(progress, "downloading", 0.0, "正在解析当前标签页的视频…")

    # 1) 页面链接优先：yt-dlp（B站/YouTube 等）
    if _is_http(page_url):
        try:
            info = _ytdlp_download(page_url, dest, progress)
            media_path = _info_filepath(info) or _find_downloaded(dest)
            if media_path is not None:
                _emit(progress, "downloading", 1.0, "下载完成")
                return _build_media_info(
                    media_path,
                    source_type="tab",
                    source_url=video_url or page_url,
                    page_url=page_url,
                    title=title,
                    provided_duration=provided_duration,
                    info=info,
                    poster=poster,
                    dest=dest,
                )
            errors.append("页面链接下载后未找到媒体文件")
        except IngestError as exc:
            logger.warning("tab 页面链接解析失败：%s", exc)
            errors.append(str(exc))

    # 2) http(s) 直链兜底
    if _is_http(video_url):
        info2: Optional[Dict[str, Any]] = None
        media_path2: Optional[Path] = None
        if _is_playlist_url(video_url):
            try:
                info2 = _ytdlp_download(video_url, dest, progress, single_file=False)
                media_path2 = _info_filepath(info2) or _find_downloaded(dest)
            except IngestError as exc:
                logger.warning("tab 直链（流媒体列表）yt-dlp 解析失败：%s", exc)
                errors.append(str(exc))
        if media_path2 is None:
            try:
                media_path2 = _direct_download(video_url, dest, progress, page_url=page_url or None)
            except IngestError as exc:
                logger.warning("tab 直链下载失败：%s", exc)
                errors.append(str(exc))
        if media_path2 is None:
            # 直链下载失败时再交给 yt-dlp 试一次（处理 HLS/DASH 伪装成普通链接）
            try:
                info2 = _ytdlp_download(video_url, dest, progress)
                media_path2 = _info_filepath(info2) or _find_downloaded(dest)
            except IngestError as exc:
                logger.warning("tab 直链 yt-dlp 兜底失败：%s", exc)
                errors.append(str(exc))
        if media_path2 is not None:
            _emit(progress, "downloading", 1.0, "下载完成")
            return _build_media_info(
                media_path2,
                source_type="tab",
                source_url=video_url,
                page_url=page_url or video_url,
                title=title,
                provided_duration=provided_duration,
                info=info2,
                poster=poster,
                dest=dest,
            )

    # 3) 都失败：给出中文指引
    if video_url.lower().startswith("blob:") or not video_url:
        raise IngestError(BLOB_HINT)
    detail = f"（{'；'.join(errors[:2])[:240]}）" if errors else ""
    raise IngestError(
        "无法解析该网页的视频"
        + detail
        + "。该页面可能是 blob/DRM 受保护的流媒体，请改用『下载视频后拖入网页』或换用视频页面链接。"
    )


def resolve_source(
    source: Dict[str, Any],
    *,
    dest_dir: str,
    progress: Optional[ProgressFn] = None,
) -> MediaInfo:
    """按 ``source`` 字典解析出可本地读取的 :class:`MediaInfo`。"""
    dest = Path(str(dest_dir)).expanduser()
    try:
        dest = dest.resolve()
    except OSError:  # pragma: no cover
        pass
    dest.mkdir(parents=True, exist_ok=True)

    payload = dict(source or {})
    source_type = str(payload.get("source_type") or "file").strip().lower()
    if source_type == "file":
        return _resolve_file(payload, dest, progress)
    if source_type == "url":
        return _resolve_url(payload, dest, progress)
    if source_type == "tab":
        return _resolve_tab(payload, dest, progress)
    raise IngestError(f"不支持的来源类型：{source_type}（仅支持 file / url / tab）")
