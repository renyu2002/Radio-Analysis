"""FastAPI 应用：HTTP API、SSE 流式问答、静态站点托管。

严格遵循 docs/CONTRACT.md §8：
- 所有响应 JSON（UTF-8），错误统一 ``{"detail": "中文错误消息"}``；
- 媒体端点支持 HTTP Range（206），供 ``<video>`` 拖动定位；
- ``/api/*`` 优先于静态文件；``GET /`` 返回 ``web/index.html``，未知路径回退 SPA；
- SSE 事件：delta → citations → done / error；
- CORS 允许 chrome-extension:// 与本地来源。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import config, store
from ..models import (
    ChatRequest,
    Citation,
    Job,
    OcrResult,
    SettingsUpdate,
    Summary,
    Transcript,
)
from ..settings import load_settings, update_settings
from .jobs import (
    JobBusyError,
    JobManager,
    JobManagerError,
    JobNotFoundError,
    has_cjk,
    uploads_dir,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_MEDIA_TYPES: Dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_EMPTY_KEY_MESSAGE = "尚未配置 DeepSeek API Key，请在设置中填写。"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _now() -> str:
    return store.now_iso()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _format_ts(seconds: Any) -> str:
    """秒 → ``MM:SS`` 或 ``HH:MM:SS``（时间戳展示）。"""
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_srt_ts(seconds: Any) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        value = 0.0
    millis = int(round(value * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _guess_media_type(path: Path) -> str:
    return (
        _MEDIA_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def _safe_upload_name(filename: str) -> str:
    """把上传文件名收敛成安全文件名（保留扩展名）。"""
    base = Path(str(filename or "")).name
    base = _SAFE_NAME_RE.sub("_", base).strip().strip(".") or "upload"
    if len(base) > 120:
        suffix = Path(base).suffix[:16]
        base = base[: 120 - len(suffix)] + suffix
    return base


def _content_disposition(filename: str, fallback: str) -> str:
    ascii_name = _SAFE_NAME_RE.sub("_", fallback) or "export"
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii") or "export"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _read_artifact(job_dir: Path, filename: str) -> Optional[Dict[str, Any]]:
    """读取 job_dir 下的 JSON 产物；存在但损坏时抛 500。"""
    path = job_dir / filename
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取产物 %s 失败", path, exc_info=True)
        raise HTTPException(status_code=500, detail="产物文件读取失败，请重新分析该视频") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="产物文件格式异常，请重新分析该视频")
    return data


def _resolve_media_path(job_dir: Path) -> Optional[Path]:
    """找到作业可播放的本地媒体文件。"""
    media_json = job_dir / "media.json"
    if media_json.is_file():
        try:
            data = json.loads(media_json.read_text(encoding="utf-8"))
            raw = str((data or {}).get("path") or "").strip()
            if raw:
                candidate = Path(raw)
                if candidate.is_file():
                    return candidate
        except (OSError, ValueError):
            logger.warning("media.json 解析失败：%s", media_json, exc_info=True)
    for pattern in (
        "source.*",
        "*.mp4",
        "*.webm",
        "*.mkv",
        "*.mov",
        "*.m4v",
        "*.flv",
        "*.mp3",
        "*.m4a",
        "*.wav",
    ):
        try:
            candidates = sorted(p for p in job_dir.glob(pattern) if p.is_file())
        except OSError:  # pragma: no cover
            continue
        if candidates:
            return candidates[0]
    return None


def _range_response(path: Path, request: Request) -> Response:
    """返回媒体响应：无 Range → 200 全量；合法 Range → 206（单区间）。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail="媒体文件不存在") from exc
    media_type = _guess_media_type(path)
    range_header = (request.headers.get("range") or "").strip()
    if not range_header:
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"}
        )

    match = _RANGE_RE.fullmatch(range_header)
    invalid = JSONResponse(
        status_code=416,
        content={"detail": "请求的字节范围无效"},
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )
    if match is None or size <= 0:
        return invalid
    start_raw, end_raw = match.group(1), match.group(2)
    if not start_raw and not end_raw:
        return invalid
    if not start_raw:  # 后缀区间：bytes=-500
        try:
            suffix = int(end_raw)
        except ValueError:
            return invalid
        if suffix <= 0:
            return invalid
        start = max(0, size - suffix)
        end = size - 1
    else:
        try:
            start = int(start_raw)
        except ValueError:
            return invalid
        if start >= size:
            return invalid
        if end_raw:
            try:
                end = int(end_raw)
            except ValueError:
                return invalid
            end = min(end, size - 1)
        else:
            end = size - 1
    if end < start:
        return invalid
    length = end - start + 1

    def iter_file() -> Iterator[bytes]:
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


def _validation_detail(exc: RequestValidationError) -> str:
    """把 pydantic 校验错误转成中文提示。"""
    translations = {
        "Field required": "缺少必填字段",
        "Input should be a valid string": "应为字符串",
        "Input should be a valid integer": "应为整数",
        "Input should be a valid number": "应为数字",
        "Input should be a valid boolean": "应为布尔值",
        "Input should be a valid list": "应为列表",
        "Input should be a valid dictionary or object to extract fields from": "应为 JSON 对象",
        "String should have at least 1 character": "不能为空",
    }
    parts: List[str] = []
    errors = []
    try:
        errors = list(exc.errors())
    except Exception:  # noqa: BLE001 - pragma: no cover
        errors = []
    for error in errors[:3]:
        location = [
            str(item)
            for item in error.get("loc", [])
            if item not in ("body", "query", "path", "header", "cookie")
        ]
        field = ".".join(location)
        message = str(error.get("msg") or "参数错误")
        message = translations.get(message, message)
        parts.append(f"{field}: {message}" if field else message)
    if not parts:
        return "请求参数错误"
    return "参数错误：" + "；".join(parts)


# ---------------------------------------------------------------------------
# ASR 轻量探测（health 用，绝不能加载 whisper 模型）
# ---------------------------------------------------------------------------

_asr_lock = threading.Lock()


def _fallback_asr(settings: Any) -> Dict[str, Any]:
    return {
        "device": "cpu",
        "model": getattr(settings, "whisper_model", "large-v3"),
        "compute_type": "int8",
        "cuda_available": False,
    }


def _probe_asr_sync(settings: Any, state: Dict[str, Any]) -> None:
    """后台线程里轻量判断 ASR 设备（core.asr.detect_device，不加载模型）。"""
    try:
        from ..core.asr import detect_device

        device, compute_type = detect_device(settings)
        device_text = str(device or "cpu")
        compute_text = str(compute_type or ("float16" if device_text.startswith("cuda") else "int8"))
        info = {
            "device": device_text,
            "model": str(getattr(settings, "whisper_model", "large-v3")),
            "compute_type": compute_text,
            "cuda_available": device_text.lower().startswith("cuda"),
        }
        ok = True
    except Exception:  # noqa: BLE001 - core 未就绪/无 GPU 均降级展示
        logger.info("ASR 设备检测不可用，health 按 CPU 展示", exc_info=True)
        info = _fallback_asr(settings)
        ok = False
    with _asr_lock:
        state["info"] = info
        state["ok"] = ok
        state["checked_at"] = time.monotonic()
        state["probing"] = False


def _start_asr_probe(app: FastAPI, settings: Any) -> Optional[threading.Thread]:
    state: Dict[str, Any] = app.state.asr_state
    with _asr_lock:
        if state.get("probing"):
            return None
        state["probing"] = True
    thread = threading.Thread(
        target=_probe_asr_sync, args=(settings, state), name="asr-probe", daemon=True
    )
    thread.start()
    return thread


def _asr_snapshot(app: FastAPI, settings: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = app.state.asr_state
    with _asr_lock:
        info = dict(state["info"]) if state.get("info") else None
        ok = bool(state.get("ok"))
        checked_at = float(state.get("checked_at") or 0.0)
        probing = bool(state.get("probing"))
    now = time.monotonic()
    stale = info is None or (not ok and now - checked_at > 60)
    if stale and not probing:
        _start_asr_probe(app, settings)
    return info or _fallback_asr(settings)


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(manager: Optional[JobManager] = None) -> FastAPI:
    """创建 FastAPI 应用（测试可注入独立 JobManager）。"""
    mgr = manager or JobManager()

    config.ensure_dirs()
    store.init_db()
    try:
        mgr.recover()
    except Exception:  # noqa: BLE001 - 恢复失败不应阻止服务启动
        logger.exception("恢复中断作业失败")

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        config.ensure_dirs()
        store.init_db()
        try:
            mgr.recover()
        except Exception:  # noqa: BLE001
            logger.exception("恢复中断作业失败")
        settings = load_settings()
        app.state.port = getattr(app.state, "port", None) or settings.port
        thread = _start_asr_probe(app, settings)
        if thread is not None:
            # 有界等待：让首次 health 尽量拿到真实设备信息，但不阻塞启动
            thread.join(timeout=2.0)
        try:
            yield
        finally:
            # 不主动关闭线程池：测试中同一进程可能多次进入 TestClient 上下文，
            # 而真实的退出清理由解释器 atexit 完成（作业线程均为空闲/短任务）。
            logger.debug("服务端应用生命周期结束")

    app = FastAPI(
        title="视频分析 · DeepSeek",
        description="本地视频/网页视频 转写、总结与问答",
        version=config.VERSION,
        lifespan=lifespan,
    )
    app.state.manager = mgr
    app.state.asr_state = {
        "info": None,
        "ok": False,
        "checked_at": 0.0,
        "probing": False,
    }
    app.state.port = config.DEFAULT_PORT

    # CORS：本地工具，允许任意来源（含 chrome-extension://）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Disposition"],
    )

    # ---------------- 异常处理：统一中文 JSON ----------------
    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError):  # noqa: ANN202
        return JSONResponse(status_code=400, content={"detail": _validation_detail(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException):  # noqa: ANN202
        detail = exc.detail
        if detail in ("Not Found", 404):
            detail = "接口不存在"
        elif detail == "Method Not Allowed":
            detail = "请求方法不支持"
        elif detail == "Internal Server Error":
            detail = "服务器内部错误"
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=getattr(exc, "headers", None),
        )

    # ---------------- 工具：作业上下文 ----------------
    def _job_context(job_id: str) -> Tuple[Job, Any, Path]:
        row = store.get_job_row(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="作业不存在")
        job = store.row_to_job(row)
        job_dir = Path(str(row["job_dir"] or (Path(config.JOBS_DIR) / job.id)))
        return job, row, job_dir

    # ---------------- §8.1 健康检查 ----------------
    @app.get("/api/health")
    def health(request: Request) -> Dict[str, Any]:
        settings = load_settings()
        try:
            configured = bool(settings.deepseek_api_key.strip())
        except Exception:  # noqa: BLE001 - pragma: no cover
            configured = False
        asr = _asr_snapshot(request.app, settings)
        port = request.url.port or getattr(request.app.state, "port", None) or settings.port
        return {
            "ok": True,
            "version": config.VERSION,
            "deepseek_configured": configured,
            "asr": asr,
            "port": port,
        }

    # ---------------- §8.2 上传文件创建作业 ----------------
    @app.post("/api/jobs/upload")
    async def create_job_upload(
        file: UploadFile = File(..., description="视频文件"),
        title: Optional[str] = Form(None),
    ) -> Dict[str, Any]:
        original_name = (file.filename or "").strip()
        if not original_name:
            raise HTTPException(status_code=400, detail="上传文件名为空，请重新选择视频文件")
        target_dir = uploads_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_upload_name(original_name)
        destination = target_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        total = 0
        try:
            with destination.open("wb") as handle:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    handle.write(chunk)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存上传文件失败")
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"保存上传文件失败：{exc}") from exc
        finally:
            try:
                await file.close()
            except Exception:  # noqa: BLE001 - pragma: no cover
                pass
        if total <= 0:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="上传文件为空，请重新选择视频文件")
        display_title = (title or "").strip() or Path(original_name).stem or safe_name
        source = {
            "source_type": "file",
            "file_path": str(destination.resolve()),
            "title": display_title,
            "upload_name": original_name,
        }
        job = mgr.create_job(
            source_type="file",
            source=source,
            title=display_title,
        )
        return {"job": job}

    # ---------------- §8.3 URL / 标签页 创建作业 ----------------
    @app.post("/api/jobs")
    def create_job(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:  # noqa: ANN401
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        source_type = str(body.get("source_type") or "").strip().lower()
        if source_type not in ("url", "tab"):
            if body.get("page_url") or body.get("video_url"):
                source_type = "tab"
            elif body.get("url"):
                source_type = "url"
            else:
                raise HTTPException(
                    status_code=400,
                    detail="缺少视频来源：请提供 url 或 page_url / video_url",
                )

        if source_type == "url":
            url = str(body.get("url") or body.get("source_url") or "").strip()
            if not url:
                raise HTTPException(status_code=400, detail="缺少视频链接：url 不能为空")
            if not re.match(r"^https?://", url, re.IGNORECASE):
                raise HTTPException(status_code=400, detail="视频链接必须以 http:// 或 https:// 开头")
            title = str(body.get("title") or "").strip()
            source = {"source_type": "url", "url": url}
            job = mgr.create_job(source_type="url", source=source, title=title, source_url=url)
            return {"job": job}

        page_url = str(body.get("page_url") or "").strip()
        video_url = str(body.get("video_url") or "").strip() or None
        if not page_url and not video_url:
            raise HTTPException(
                status_code=400,
                detail="缺少页面链接：page_url 与 video_url 至少提供一个",
            )
        title = str(body.get("title") or "").strip()
        duration = max(0.0, _as_float(body.get("duration")))
        source = {
            "source_type": "tab",
            "page_url": page_url,
            "video_url": video_url,
            "title": title,
            "duration": duration,
            "poster": body.get("poster"),
        }
        job = mgr.create_job(
            source_type="tab",
            source=source,
            title=title or page_url,
            source_url=page_url or video_url,
            duration=duration,
        )
        return {"job": job}

    # ---------------- §8.4 作业列表 ----------------
    @app.get("/api/jobs")
    def list_jobs(limit: int = 50) -> Dict[str, Any]:
        if limit <= 0:
            limit = 50
        limit = min(limit, 200)
        return {"jobs": mgr.list_jobs(limit)}

    # ---------------- §8.5 作业详情 ----------------
    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Dict[str, Any]:
        job = mgr.get_job(job_id, include_summary=True)
        if job is None:
            raise HTTPException(status_code=404, detail="作业不存在")
        return {"job": job}

    # ---------------- §8.6 转写 ----------------
    @app.get("/api/jobs/{job_id}/transcript")
    def get_transcript(job_id: str) -> Dict[str, Any]:
        _job, _row, job_dir = _job_context(job_id)
        data = _read_artifact(job_dir, "transcript.json")
        if data is None:
            raise HTTPException(status_code=404, detail="转写尚未完成")
        try:
            transcript = Transcript.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="转写文件格式异常，请重新分析该视频") from exc
        return {"transcript": transcript}

    # ---------------- §8.7 总结 ----------------
    @app.get("/api/jobs/{job_id}/summary")
    def get_summary(job_id: str) -> Dict[str, Any]:
        _job, _row, job_dir = _job_context(job_id)
        data = _read_artifact(job_dir, "summary.json")
        if data is None:
            raise HTTPException(status_code=404, detail="总结尚未生成")
        try:
            summary = Summary.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="总结文件格式异常，请重新生成总结") from exc
        return {"summary": summary}

    # ---------------- §8.8 画面文字 ----------------
    @app.get("/api/jobs/{job_id}/ocr")
    def get_ocr(job_id: str) -> Dict[str, Any]:
        _job, _row, job_dir = _job_context(job_id)
        data = _read_artifact(job_dir, "ocr.json")
        if data is None:
            raise HTTPException(status_code=404, detail="画面文字识别结果尚未生成")
        try:
            ocr = OcrResult.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="画面文字文件格式异常，请重新分析该视频") from exc
        return {"ocr": ocr}

    # ---------------- §8.9 媒体流（支持 Range） ----------------
    @app.get("/api/jobs/{job_id}/media")
    def get_media(job_id: str, request: Request) -> Response:
        _job, _row, job_dir = _job_context(job_id)
        media_path = _resolve_media_path(job_dir)
        if media_path is None:
            raise HTTPException(status_code=404, detail="媒体文件尚未就绪")
        return _range_response(media_path, request)

    # ---------------- §8.10 缩略图 ----------------
    @app.get("/api/jobs/{job_id}/thumbnail")
    def get_thumbnail(job_id: str) -> Response:
        _job, _row, job_dir = _job_context(job_id)
        thumbnail = job_dir / "thumbnail.jpg"
        if not thumbnail.is_file():
            raise HTTPException(status_code=404, detail="缩略图不存在")
        return FileResponse(
            thumbnail,
            media_type="image/jpeg",
            headers={"Cache-Control": "max-age=60"},
        )

    # ---------------- §8.11 关键帧 ----------------
    @app.get("/api/jobs/{job_id}/frames/{filename}")
    def get_frame(job_id: str, filename: str) -> Response:
        _job, _row, job_dir = _job_context(job_id)
        name = str(filename or "").strip()
        if (
            not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or Path(name).name != name
        ):
            raise HTTPException(status_code=404, detail="关键帧不存在")
        suffix = Path(name).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png"):
            raise HTTPException(status_code=404, detail="关键帧不存在")
        frames_dir = (job_dir / "frames").resolve()
        target = (frames_dir / name).resolve()
        try:
            target.relative_to(frames_dir)
        except ValueError:
            raise HTTPException(status_code=404, detail="关键帧不存在") from None
        if not target.is_file():
            raise HTTPException(status_code=404, detail="关键帧不存在")
        media_type = "image/png" if suffix == ".png" else "image/jpeg"
        return FileResponse(target, media_type=media_type, headers={"Cache-Control": "max-age=60"})

    # ---------------- §8.12 重新生成总结 ----------------
    @app.post("/api/jobs/{job_id}/regenerate-summary")
    def regenerate_summary(job_id: str) -> Dict[str, Any]:
        _job, _row, job_dir = _job_context(job_id)
        settings = load_settings()
        if not settings.deepseek_api_key.strip():
            raise HTTPException(status_code=400, detail=_EMPTY_KEY_MESSAGE)
        if not (job_dir / "transcript.json").is_file():
            raise HTTPException(
                status_code=400, detail="该作业尚未完成转写，无法重新生成总结"
            )
        try:
            job = mgr.start_regenerate(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": job}

    # ---------------- §8.13 消息列表 ----------------
    @app.get("/api/jobs/{job_id}/messages")
    def list_messages(job_id: str) -> Dict[str, Any]:
        _job_context(job_id)
        return {"messages": store.list_messages(job_id)}

    # ---------------- §8.14 问答（流式 / 非流式） ----------------
    def _load_qa() -> Tuple[Any, Any, Any]:
        try:
            from ..core.deepseek import DeepSeekError
            from ..core.qa import answer_once, answer_stream
        except Exception as exc:  # noqa: BLE001 - core 未就绪
            logger.exception("导入 core.qa 失败")
            raise HTTPException(
                status_code=500,
                detail=f"问答引擎尚未就绪（core 模块未实现或依赖缺失），请稍后重启服务重试。（{exc}）",
            ) from exc
        return answer_stream, answer_once, DeepSeekError

    def _map_qa_error(exc: Exception, deepseek_error_cls: Any) -> HTTPException:  # noqa: ANN401
        message = str(exc or "").strip() or "问答失败，请稍后重试"
        if deepseek_error_cls is not None and isinstance(exc, deepseek_error_cls):
            if "API Key" in message or "401" in message or "未配置" in message:
                return HTTPException(status_code=400, detail=message)
            return HTTPException(status_code=502, detail=message)
        if not has_cjk(message):
            message = f"问答失败：{message}"
        return HTTPException(status_code=500, detail=message)

    def _chat_sse_stream(
        job_id: str, iterator: Any, citations: Any  # noqa: ANN401
    ) -> Iterator[str]:
        parts: List[str] = []
        try:
            for chunk in iterator:
                text = "" if chunk is None else str(chunk)
                if not text:
                    continue
                parts.append(text)
                yield _sse("delta", {"text": text})
        except Exception as exc:  # noqa: BLE001 - 流中途失败发 error 事件
            logger.exception("作业 %s 流式回答失败", job_id)
            message = str(exc or "").strip() or "回答生成失败，请稍后重试"
            if not has_cjk(message):
                message = f"回答生成失败：{message}"
            yield _sse("error", {"message": message})
            return
        try:
            items = [
                item.model_dump() if isinstance(item, Citation) else dict(item)
                for item in (citations or [])
            ]
            yield _sse("citations", {"items": items})
            message = store.add_message(job_id, "assistant", "".join(parts), citations or [])
            yield _sse("done", {"message_id": message.id})
        except Exception:  # noqa: BLE001
            logger.exception("作业 %s 保存回答失败", job_id)
            yield _sse("error", {"message": "回答已生成，但保存到数据库失败，请重试。"})

    @app.post("/api/jobs/{job_id}/chat")
    def chat(job_id: str, request: ChatRequest) -> Response:
        _job, _row, job_dir = _job_context(job_id)
        question = (request.question or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="问题不能为空")
        settings = load_settings()
        if not settings.deepseek_api_key.strip():
            raise HTTPException(status_code=400, detail=_EMPTY_KEY_MESSAGE)
        if not (job_dir / "transcript.json").is_file():
            raise HTTPException(
                status_code=400, detail="该作业尚未完成转写，暂时无法问答，请等待分析完成"
            )
        answer_stream, answer_once, deepseek_error_cls = _load_qa()
        history = [
            {"role": message.role, "content": message.content}
            for message in store.list_messages(job_id)
        ][-8:]

        if request.stream:
            try:
                iterator, citations = answer_stream(
                    job_dir=str(job_dir),
                    question=question,
                    history=history,
                    settings=settings,
                )
            except Exception as exc:  # noqa: BLE001
                raise _map_qa_error(exc, deepseek_error_cls) from exc
            store.add_message(job_id, "user", question)
            return StreamingResponse(
                _chat_sse_stream(job_id, iterator, citations),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        try:
            answer, citations = answer_once(
                job_dir=str(job_dir),
                question=question,
                history=history,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_qa_error(exc, deepseek_error_cls) from exc
        store.add_message(job_id, "user", question)
        assistant = store.add_message(job_id, "assistant", answer or "", citations or [])
        return JSONResponse(
            content={
                "answer": answer or "",
                "citations": [
                    item.model_dump() if isinstance(item, Citation) else dict(item)
                    for item in (citations or [])
                ],
                "message": assistant.model_dump(),
            }
        )

    # ---------------- §8.15 删除作业 ----------------
    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> Dict[str, Any]:
        if not mgr.delete_job(job_id):
            raise HTTPException(status_code=404, detail="作业不存在")
        return {"ok": True}

    # ---------------- §8.16 / §8.17 设置 ----------------
    @app.get("/api/settings")
    def get_settings() -> Dict[str, Any]:
        return {"settings": load_settings().to_view()}

    @app.put("/api/settings")
    def put_settings(update: SettingsUpdate) -> Dict[str, Any]:
        try:
            settings = update_settings(update)
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存设置失败")
            raise HTTPException(status_code=500, detail=f"保存设置失败：{exc}") from exc
        logger.info("设置已更新")
        return {"settings": settings.to_view()}

    # ---------------- §8.18 测试 DeepSeek 连接 ----------------
    @app.post("/api/settings/test-deepseek")
    def test_deepseek() -> Dict[str, Any]:
        settings = load_settings()
        model = settings.deepseek_model
        if not settings.deepseek_api_key.strip():
            return {"ok": False, "message": _EMPTY_KEY_MESSAGE, "model": model}
        try:
            from ..core.deepseek import test_connection
        except Exception as exc:  # noqa: BLE001 - core 未就绪
            logger.exception("导入 core.deepseek 失败")
            return {
                "ok": False,
                "message": f"DeepSeek 模块尚未就绪（core 未实现），暂时无法测试连接。（{exc}）",
                "model": model,
            }
        try:
            ok, message = test_connection(settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("测试 DeepSeek 连接异常")
            return {"ok": False, "message": f"测试连接失败：{exc}", "model": model}
        text = str(message or ("连接成功" if ok else "连接失败")).strip()
        return {"ok": bool(ok), "message": text, "model": model}

    # ---------------- §8.19 测试视觉模型（可选） ----------------
    @app.post("/api/settings/test-vision")
    def test_vision() -> Dict[str, Any]:
        settings = load_settings()
        model = settings.vision_model
        if not settings.vision_enabled:
            return {"ok": False, "message": "视觉模型未启用（可选功能，默认关闭）", "model": model}
        if not (settings.vision_base_url and settings.vision_model and settings.vision_api_key):
            return {
                "ok": False,
                "message": "视觉模型配置不完整，请填写 Base URL、模型与 API Key",
                "model": model,
            }
        try:
            import httpx

            url = settings.vision_base_url.rstrip("/") + "/chat/completions"
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {settings.vision_api_key}"},
                json={
                    "model": settings.vision_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"连接失败：{exc}", "model": model}
        if response.status_code == 200:
            return {"ok": True, "message": "连接成功", "model": model}
        return {
            "ok": False,
            "message": f"连接失败（HTTP {response.status_code}）：{response.text[:200]}",
            "model": model,
        }

    # ---------------- §8.21 导出（可选） ----------------
    @app.get("/api/jobs/{job_id}/export")
    def export_job(job_id: str, format: str = "txt") -> Response:  # noqa: A002
        job, _row, job_dir = _job_context(job_id)
        fmt = str(format or "txt").strip().lower()
        if fmt not in ("txt", "srt", "md"):
            raise HTTPException(status_code=400, detail="不支持的导出格式，仅支持 txt / srt / md")
        if fmt in ("txt", "srt"):
            data = _read_artifact(job_dir, "transcript.json")
            if data is None:
                raise HTTPException(status_code=404, detail="转写尚未完成，无法导出")
            try:
                transcript = Transcript.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500, detail="转写文件格式异常，无法导出") from exc
            if fmt == "txt":
                content = transcript.text or "\n".join(
                    f"[{_format_ts(segment.start)}] {segment.text}"
                    for segment in transcript.segments
                )
                suffix, media_type = ".txt", "text/plain; charset=utf-8"
            else:
                blocks: List[str] = []
                for index, segment in enumerate(transcript.segments, start=1):
                    blocks.append(
                        f"{index}\n{_format_srt_ts(segment.start)} --> {_format_srt_ts(segment.end)}\n"
                        f"{segment.text}\n"
                    )
                content = "\n".join(blocks)
                suffix, media_type = ".srt", "application/x-subrip; charset=utf-8"
        else:
            data = _read_artifact(job_dir, "summary.json")
            if data is None:
                raise HTTPException(status_code=404, detail="总结尚未生成，无法导出")
            try:
                summary = Summary.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500, detail="总结文件格式异常，无法导出") from exc
            content = _summary_markdown(summary)
            suffix, media_type = ".md", "text/markdown; charset=utf-8"

        title = (job.title or "").strip() or job_id
        filename = f"{title}{suffix}"
        return Response(
            content=content.encode("utf-8"),
            media_type=media_type,
            headers={"Content-Disposition": _content_disposition(filename, f"{job_id}{suffix}")},
        )

    # ---------------- §8.20 静态站点（API 优先，SPA fallback） ----------------
    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_static(full_path: str) -> Response:
        web_dir = Path(config.WEB_DIR)
        index_file = web_dir / "index.html"
        relative = (full_path or "").strip("/")
        if relative:
            try:
                candidate = (web_dir / relative).resolve()
                web_root = web_dir.resolve()
            except (OSError, ValueError):
                raise HTTPException(status_code=404, detail="文件不存在") from None
            try:
                candidate.relative_to(web_root)
            except ValueError:
                raise HTTPException(status_code=404, detail="文件不存在") from None
            if candidate.is_file():
                return FileResponse(
                    candidate,
                    media_type=mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            if relative.startswith("api/") or relative == "api":
                raise HTTPException(status_code=404, detail="接口不存在")
            if Path(relative).suffix:
                raise HTTPException(status_code=404, detail="文件不存在")
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="前端页面尚未构建，请稍后刷新")
        return FileResponse(
            index_file, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"}
        )

    return app


def _summary_markdown(summary: Summary) -> str:
    """Summary → Markdown 文本（导出用）。"""
    lines: List[str] = [f"# {summary.title or '视频总结'}", ""]
    if summary.one_liner:
        lines.extend([f"> {summary.one_liner}", ""])
    if summary.overview:
        lines.extend([summary.overview, ""])
    if summary.chapters:
        lines.extend(["## 章节", ""])
        for chapter in summary.chapters:
            lines.append(
                f"- **{_format_ts(chapter.start)} - {_format_ts(chapter.end)}** "
                f"{chapter.title}：{chapter.summary}"
            )
        lines.append("")
    if summary.key_points:
        lines.extend(["## 要点", ""])
        lines.extend(f"- {point}" for point in summary.key_points)
        lines.append("")
    if summary.quotes:
        lines.extend(["## 金句", ""])
        lines.extend(f"> {quote_text}" for quote_text in summary.quotes)
        lines.append("")
    if summary.topics:
        lines.extend(["## 主题", "", "、".join(summary.topics), ""])
    if summary.keywords:
        lines.extend(["## 关键词", "", "、".join(summary.keywords), ""])
    if summary.visual_notes:
        lines.extend(["## 画面线索", ""])
        lines.extend(f"- {note}" for note in summary.visual_notes)
        lines.append("")
    if summary.generated_by:
        lines.extend([f"*由 {summary.generated_by} 生成于 {summary.created_at}*", ""])
    return "\n".join(lines).strip() + "\n"


#: 模块级应用实例（uvicorn / `python -m vanalyze` 使用）
app = create_app()
