"""作业管理器：单 worker 线程池、阶段状态机、进度节流、启动恢复、重新生成总结。

契约（docs/CONTRACT.md §7 / §9）：
- ``ThreadPoolExecutor(max_workers=1)``（Whisper 占显存，串行最稳）；
- ``progress(stage_key, pct, message)`` 回调更新 DB + 内存，写入节流 ≤5 次/秒；
- 阶段列表见 §7，``Job.stages`` 是“全部阶段状态”；
- 服务启动时把残留的 pending/running 作业标记为 error（中文消息）；
- 删除作业先删 DB 再 rmtree(job_dir)；
- 上传文件存 ``data/uploads/``。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, store
from ..models import Job, StageState
from ..settings import Settings, load_settings

logger = logging.getLogger(__name__)

#: 阶段 key → 中文标签（§7）
STAGE_LABELS: Dict[str, str] = {
    "queued": "排队中",
    "resolving": "解析视频信息",
    "downloading": "下载视频",
    "probing": "读取媒体信息",
    "transcribing": "语音转文字",
    "frames": "抽取画面关键帧",
    "ocr": "识别画面文字",
    "analyzing": "生成内容总结",
    "done": "完成",
    "error": "出错",
}

#: 终态阶段（收到这类 progress 时忽略，由 _succeed/_fail 统一收尾）
_TERMINAL_STAGES = {"done", "error"}

#: 进度写库最小间隔（秒）→ ≤5 次/秒
_PROGRESS_INTERVAL = 0.2

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def uploads_dir() -> Path:
    """上传文件目录：``data/uploads/``。"""
    return Path(config.DATA_DIR) / "uploads"


def stage_plan(source_type: str) -> List[str]:
    """按来源类型给出该作业会经历的阶段（不含 done/error 终态）。"""
    plan = ["queued", "resolving"]
    if source_type in ("url", "tab"):
        plan.append("downloading")
    plan.extend(["probing", "transcribing", "frames", "ocr", "analyzing"])
    return plan


def new_stages(source_type: str) -> List[StageState]:
    """初始阶段列表：queued 运行中，其余 pending。"""
    keys = stage_plan(source_type)
    return [
        StageState(
            key=key,
            label=STAGE_LABELS.get(key, key),
            status="running" if key == "queued" else "pending",
            progress=0.0,
        )
        for key in keys
    ]


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _overall_progress(stages: List[StageState]) -> float:
    """作业总体进度 = (已完成阶段数 + 当前阶段进度) / 总阶段数。"""
    total = len(stages)
    if total <= 0:
        return 0.0
    done = sum(1 for s in stages if s.status == "done")
    running = next((s for s in stages if s.status == "running"), None)
    current = running.progress if running is not None else 0.0
    return _clamp((done + current) / total)


def _stages_dump(stages: List[StageState]) -> List[Dict[str, Any]]:
    return [s.model_dump() for s in stages]


def _safe_source_signature(source_type: str, source: Dict[str, Any]) -> str:
    """用于防止同一来源重复提交。"""
    try:
        if source_type == "file":
            path = str(source.get("file_path") or "").strip()
            return f"file:{path.lower()}" if path else ""
        if source_type == "url":
            url = str(source.get("url") or "").strip().lower()
            return f"url:{url}" if url else ""
        if source_type == "tab":
            page = str(source.get("page_url") or "").strip().lower()
            video = str(source.get("video_url") or "").strip().lower()
            key = page or video
            return f"tab:{key}" if key else ""
    except Exception:  # noqa: BLE001 - 去重失败不影响主流程
        return ""
    return ""


def friendly_error(exc: BaseException) -> str:
    """把异常转换成可直接展示给用户的中文消息。"""
    message = str(exc or "").strip()
    if isinstance(exc, FileNotFoundError):
        return "找不到媒体文件，请确认文件仍然存在后重新分析。"
    if isinstance(exc, MemoryError):
        return "内存不足，请关闭其他程序后重试。"
    if isinstance(exc, PermissionError):
        return f"没有权限访问文件或目录：{message}"
    if not message:
        return "处理失败：未知错误，请重试。"
    if has_cjk(message):
        return message
    return f"处理失败：{message}"


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _media_meta(job_dir: Path) -> Dict[str, Any]:
    """从 media.json 读取标题/时长（容错）。"""
    data = _read_json_file(job_dir / "media.json")
    if not data:
        return {}
    title = str(data.get("title") or "").strip()
    try:
        duration = float(data.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"title": title, "duration": duration}


def _count_segments(job_dir: Path) -> int:
    data = _read_json_file(job_dir / "transcript.json")
    if not data:
        return 0
    segments = data.get("segments")
    return len(segments) if isinstance(segments, list) else 0


class JobManagerError(Exception):
    """作业管理器的业务错误基类（str 为中文，可展示）。"""


class JobNotFoundError(JobManagerError):
    """作业不存在。"""


class JobBusyError(JobManagerError):
    """作业正在处理中，状态不允许该操作。"""


class JobManager:
    """内存 + SQLite 双层状态的作业管理器。"""

    def __init__(self, max_workers: int = 1) -> None:
        config.ensure_dirs()
        uploads_dir().mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="vanalyze-job"
        )
        self._lock = threading.RLock()
        self._stages: Dict[str, List[StageState]] = {}
        self._last_write: Dict[str, float] = {}
        self._last_stage: Dict[str, str] = {}
        self._active_sig: Dict[str, str] = {}
        self._futures: Dict[str, Future[Any]] = {}
        self._cancelled: set[str] = set()
        self._meta_synced: set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def recover(self) -> int:
        """服务启动恢复：把上次中断的作业标记为 error。"""
        count = store.mark_interrupted()
        if count:
            logger.warning("已把 %d 个因服务重启而中断的作业标记为 error", count)
        return count

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._executor.shutdown(wait=wait, cancel_futures=True)

    # ------------------------------------------------------------------
    # 创建 / 查询
    # ------------------------------------------------------------------
    def create_job(
        self,
        *,
        source_type: str,
        source: Dict[str, Any],
        title: str = "",
        source_url: Optional[str] = None,
        duration: float = 0.0,
    ) -> Job:
        """创建作业并立即开始处理；同一来源的活跃作业会被复用（防重复提交）。"""
        if source_type not in ("file", "url", "tab"):
            raise JobManagerError("不支持的视频来源类型")
        settings = load_settings()
        signature = _safe_source_signature(source_type, source)
        with self._lock:
            if signature:
                existing_id = self._active_sig.get(signature)
                if existing_id:
                    row = store.get_job_row(existing_id)
                    if row is not None and str(row["status"]) in ("pending", "running"):
                        logger.info("来源重复，复用处理中的作业 %s", existing_id)
                        job = self.get_job(existing_id)
                        if job is not None:
                            return job
                    self._active_sig.pop(signature, None)

            job_id = self._new_job_id()
            job_dir = Path(config.JOBS_DIR) / job_id
            job_dir.mkdir(parents=True, exist_ok=True)

            stages = new_stages(source_type)
            created = store.now_iso()
            job = Job(
                id=job_id,
                status="pending",
                stage="queued",
                stage_label=STAGE_LABELS["queued"],
                progress=0.0,
                source_type=source_type,  # type: ignore[arg-type]
                title=title or "",
                source_url=source_url,
                duration=max(0.0, float(duration or 0.0)),
                created_at=created,
                updated_at=created,
                stages=stages,
            )
            payload = {
                "job_dir": str(job_dir),
                "source": source,
                "stages": _stages_dump(stages),
                "segment_count": 0,
            }
            store.create_job(job, payload)
            self._stages[job_id] = stages
            self._last_write[job_id] = 0.0
            self._last_stage[job_id] = "queued"
            if signature:
                self._active_sig[signature] = job_id
            try:
                future = self._executor.submit(
                    self._run_job, job_id, str(job_dir), dict(source), settings
                )
                self._futures[job_id] = future
            except RuntimeError:  # 线程池已关闭（服务退出中）
                self._fail(job_id, "服务正在关闭，作业未能启动，请稍后重试。")
            logger.info("已创建作业 %s（来源 %s）", job_id, source_type)
        return self.get_job(job_id) or job

    def _new_job_id(self) -> str:
        for _ in range(50):
            candidate = uuid.uuid4().hex[:12]
            if store.get_job_row(candidate) is None:
                return candidate
        return uuid.uuid4().hex

    def get_job(self, job_id: str, include_summary: bool = False) -> Optional[Job]:
        row = store.get_job_row(job_id)
        if row is None:
            return None
        job = store.row_to_job(row)
        payload = store.row_payload(row)
        job_dir = Path(str(row["job_dir"] or (Path(config.JOBS_DIR) / job_id)))
        self._decorate(job, payload, job_dir, include_summary=include_summary)
        return job

    def list_jobs(self, limit: int = 50) -> List[Job]:
        jobs: List[Job] = []
        for row in store.list_job_rows(limit):
            job = store.row_to_job(row)
            payload = store.row_payload(row)
            job_dir = Path(str(row["job_dir"] or (Path(config.JOBS_DIR) / job.id)))
            try:
                self._decorate(job, payload, job_dir, include_summary=False)
            except Exception:  # noqa: BLE001 - 单条异常不影响列表
                logger.warning("装饰作业 %s 失败", job.id, exc_info=True)
            jobs.append(job)
        return jobs

    def get_job_row(self, job_id: str) -> Optional[Any]:
        return store.get_job_row(job_id)

    def job_dir(self, job_id: str) -> Optional[Path]:
        row = store.get_job_row(job_id)
        if row is None:
            return None
        return Path(str(row["job_dir"] or (Path(config.JOBS_DIR) / job_id)))

    def _decorate(
        self,
        job: Job,
        payload: Dict[str, Any],
        job_dir: Path,
        *,
        include_summary: bool,
    ) -> None:
        try:
            job.has_transcript = (job_dir / "transcript.json").is_file()
            job.has_summary = (job_dir / "summary.json").is_file()
            job.has_ocr = (job_dir / "ocr.json").is_file()
            has_thumb = (job_dir / "thumbnail.jpg").is_file()
        except OSError:  # pragma: no cover - 非常规文件系统
            job.has_transcript = job.has_summary = job.has_ocr = has_thumb = False

        # 阶段列表：内存中最新的优先，其次 DB payload
        with self._lock:
            stages = self._stages.get(job.id)
        if stages is not None:
            job.stages = [StageState.model_validate(s.model_dump()) for s in stages]
        elif not job.stages:
            job.stages = new_stages(job.source_type)

        segment_count = int(payload.get("segment_count") or 0)
        if job.has_transcript and segment_count <= 0:
            segment_count = _count_segments(job_dir)
            if segment_count:
                payload["segment_count"] = segment_count
                store.update_job(job.id, payload_patch={"segment_count": segment_count})
        job.segment_count = segment_count
        job.message_count = store.count_messages(job.id)

        meta = _media_meta(job_dir)
        if job.duration <= 0 and meta.get("duration"):
            job.duration = float(meta["duration"])
        if not job.title and meta.get("title"):
            job.title = str(meta["title"])

        job.media_url = f"/api/jobs/{job.id}/media"
        job.thumbnail_url = f"/api/jobs/{job.id}/thumbnail" if has_thumb else None

        if include_summary and job.has_summary:
            data = _read_json_file(job_dir / "summary.json")
            if data:
                try:
                    from ..models import Summary

                    job.summary = Summary.model_validate(data)
                except Exception:  # noqa: BLE001 - 产物损坏时降级
                    logger.warning("作业 %s 的 summary.json 解析失败", job.id, exc_info=True)
                    job.summary = None

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def delete_job(self, job_id: str) -> bool:
        """删除作业：先删 DB 记录，再清理 job_dir 与服务端上传副本。"""
        with self._lock:
            row = store.delete_job(job_id)
            if row is None:
                return False
            self._cancelled.add(job_id)
            future = self._futures.pop(job_id, None)
            if future is not None:
                future.cancel()
            self._forget(job_id)
            for signature, owner in list(self._active_sig.items()):
                if owner == job_id:
                    self._active_sig.pop(signature, None)

        job_dir = Path(str(row.get("job_dir") or (Path(config.JOBS_DIR) / job_id)))
        shutil.rmtree(job_dir, ignore_errors=True)

        # 上传的服务端副本可以一并清理；用户本机文件绝不删除
        payload = store.parse_payload(row.get("payload"))
        source = payload.get("source") if isinstance(payload, dict) else None
        if isinstance(source, dict) and source.get("source_type") == "file":
            uploaded = str(source.get("file_path") or "").strip()
            if uploaded:
                try:
                    path = Path(uploaded).resolve()
                    upload_root = uploads_dir().resolve()
                    if path.is_file() and upload_root in path.parents:
                        path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    logger.warning("清理上传副本失败：%s", uploaded, exc_info=True)
        logger.info("已删除作业 %s", job_id)
        return True

    def _forget(self, job_id: str) -> None:
        self._stages.pop(job_id, None)
        self._last_write.pop(job_id, None)
        self._last_stage.pop(job_id, None)
        self._meta_synced.discard(job_id)

    # ------------------------------------------------------------------
    # 后台执行：完整分析
    # ------------------------------------------------------------------
    def _run_job(
        self,
        job_id: str,
        job_dir: str,
        source: Dict[str, Any],
        settings: Settings,
    ) -> None:
        pipeline_error_cls: Optional[type] = None
        try:
            if job_id in self._cancelled:
                return
            if not store.update_job(job_id, {"status": "running"}):
                return
            try:
                from ..core.pipeline import PipelineError, run_pipeline
            except Exception as exc:  # noqa: BLE001 - core 未就绪/依赖缺失
                logger.exception("导入 core.pipeline 失败")
                self._fail(
                    job_id,
                    "分析引擎尚未就绪（core 模块未实现或依赖缺失），请稍后重启服务重试。"
                    f"（{exc}）",
                )
                return
            pipeline_error_cls = PipelineError

            def progress(stage: str, pct: float, message: Optional[str] = None) -> None:
                self._on_progress(job_id, stage, pct, message)

            run_pipeline(
                job_id=job_id,
                job_dir=job_dir,
                source=source,
                settings=settings,
                progress=progress,
            )
            self._succeed(job_id, job_dir)
        except BaseException as exc:  # noqa: BLE001 - 兜底，线程池不能让异常静默
            if pipeline_error_cls is not None and isinstance(exc, pipeline_error_cls):
                self._fail(job_id, str(exc) or "视频分析失败，请重试。")
            elif isinstance(exc, (KeyboardInterrupt, SystemExit)):  # pragma: no cover
                self._fail(job_id, "服务正在关闭，作业被中断，请重新分析。")
                raise
            else:
                logger.exception("作业 %s 处理失败", job_id)
                self._fail(job_id, friendly_error(exc))
        finally:
            with self._lock:
                cancelled = job_id in self._cancelled
                self._futures.pop(job_id, None)
                self._cancelled.discard(job_id)
                self._forget(job_id)
                for signature, owner in list(self._active_sig.items()):
                    if owner == job_id:
                        self._active_sig.pop(signature, None)
            if cancelled:
                # 作业在运行中被删除：分析线程可能重新创建了部分目录，收尾再清一次
                shutil.rmtree(Path(config.JOBS_DIR) / job_id, ignore_errors=True)

    # ------------------------------------------------------------------
    # 后台执行：重新生成总结
    # ------------------------------------------------------------------
    def start_regenerate(self, job_id: str) -> Job:
        """后台重新生成总结；立即返回状态为 running 的作业。"""
        settings = load_settings()
        with self._lock:
            row = store.get_job_row(job_id)
            if row is None:
                raise JobNotFoundError("作业不存在")
            if str(row["status"]) in ("pending", "running"):
                raise JobBusyError("作业正在处理中，请稍后再试")
            job = store.row_to_job(row)
            job_dir = str(row["job_dir"] or (Path(config.JOBS_DIR) / job_id))
            stages = [
                StageState(
                    key=key,
                    label=STAGE_LABELS.get(key, key),
                    status="running" if key == "analyzing" else "done",
                    progress=0.0 if key == "analyzing" else 1.0,
                )
                for key in stage_plan(job.source_type)
            ]
            self._stages[job_id] = stages
            self._last_stage[job_id] = "analyzing"
            self._last_write[job_id] = 0.0
            overall = _overall_progress(stages)
            store.update_job(
                job_id,
                {
                    "status": "running",
                    "stage": "analyzing",
                    "stage_label": STAGE_LABELS["analyzing"],
                    "progress": overall,
                    "error": None,
                },
                payload_patch={"stages": _stages_dump(stages)},
            )
            if job_id in self._cancelled:
                raise JobBusyError("作业正在删除中，请稍后再试")
            try:
                future = self._executor.submit(self._run_regenerate, job_id, job_dir, settings)
                self._futures[job_id] = future
            except RuntimeError:
                self._fail(job_id, "服务正在关闭，无法重新生成总结，请稍后重试。")
                raise JobBusyError("服务正在关闭，请稍后重试") from None
        return self.get_job(job_id) or job

    def _run_regenerate(self, job_id: str, job_dir: str, settings: Settings) -> None:
        pipeline_error_cls: Optional[type] = None
        try:
            if job_id in self._cancelled:
                return
            try:
                from ..core.pipeline import PipelineError, regenerate_summary
            except Exception as exc:  # noqa: BLE001
                logger.exception("导入 core.pipeline.regenerate_summary 失败")
                self._fail(
                    job_id,
                    "重新生成总结需要分析引擎（core 模块尚未就绪），请稍后重启服务重试。"
                    f"（{exc}）",
                )
                return
            pipeline_error_cls = PipelineError

            def progress(stage: str, pct: float, message: Optional[str] = None) -> None:
                self._on_progress(job_id, stage, pct, message)

            regenerate_summary(job_dir=job_dir, settings=settings, progress=progress)
            self._succeed(job_id, job_dir)
        except BaseException as exc:  # noqa: BLE001
            if pipeline_error_cls is not None and isinstance(exc, pipeline_error_cls):
                self._fail(job_id, str(exc) or "重新生成总结失败，请重试。")
            elif isinstance(exc, (KeyboardInterrupt, SystemExit)):  # pragma: no cover
                self._fail(job_id, "服务正在关闭，重新生成总结被中断。")
                raise
            else:
                logger.exception("作业 %s 重新生成总结失败", job_id)
                self._fail(job_id, friendly_error(exc))
        finally:
            with self._lock:
                cancelled = job_id in self._cancelled
                self._futures.pop(job_id, None)
                self._cancelled.discard(job_id)
                self._forget(job_id)
            if cancelled:
                shutil.rmtree(Path(config.JOBS_DIR) / job_id, ignore_errors=True)

    # ------------------------------------------------------------------
    # 进度 / 收尾
    # ------------------------------------------------------------------
    def _on_progress(
        self, job_id: str, stage: str, pct: float, message: Optional[str] = None
    ) -> None:
        key = str(stage or "").strip()
        if not key or key in _TERMINAL_STAGES:
            return
        with self._lock:
            if job_id in self._cancelled:
                return
            stages = self._stages.get(job_id)
            if stages is None:
                return
            progress = _clamp(pct)
            now = time.monotonic()
            stage_changed = self._last_stage.get(job_id) != key

            index = next((i for i, item in enumerate(stages) if item.key == key), -1)
            if index < 0:
                stages.append(
                    StageState(
                        key=key,
                        label=STAGE_LABELS.get(key, key),
                        status="running",
                        progress=progress,
                    )
                )
                index = len(stages) - 1
            for earlier in stages[:index]:
                if earlier.status != "done":
                    earlier.status = "done"
                    earlier.progress = 1.0
            current = stages[index]
            if current.status != "running":
                current.status = "running"
            current.progress = progress
            if key != "queued" and stages and stages[0].key == "queued":
                if stages[0].status != "done":
                    stages[0].status = "done"
                    stages[0].progress = 1.0

            overall = _overall_progress(stages)
            label = STAGE_LABELS.get(key, key)
            self._last_stage[job_id] = key

            forced = stage_changed or progress >= 0.999
            throttled = now - self._last_write.get(job_id, 0.0) >= _PROGRESS_INTERVAL
            if not (forced or throttled):
                return
            self._last_write[job_id] = now

            fields: Dict[str, Any] = {
                "stage": key,
                "stage_label": label,
                "progress": round(overall, 4),
            }
            # 首次进入读取媒体信息 / 转写阶段时，同步从 media.json 取标题与时长
            if key in ("probing", "transcribing", "analyzing") and job_id not in self._meta_synced:
                self._meta_synced.add(job_id)
                meta = _media_meta(Path(config.JOBS_DIR) / job_id)
                if meta.get("title"):
                    fields["title"] = str(meta["title"])
                if meta.get("duration"):
                    fields["duration"] = float(meta["duration"])
            store.update_job(job_id, fields, payload_patch={"stages": _stages_dump(stages)})

    def _succeed(self, job_id: str, job_dir: str) -> None:
        with self._lock:
            if job_id in self._cancelled:
                return
            stages = self._stages.get(job_id)
            stages_dump: Optional[List[Dict[str, Any]]] = None
            if stages is not None:
                for stage in stages:
                    stage.status = "done"
                    stage.progress = 1.0
                stages_dump = _stages_dump(stages)
            directory = Path(job_dir)
            media = _media_meta(directory)
            segment_count = _count_segments(directory)
            fields: Dict[str, Any] = {
                "status": "done",
                "stage": "done",
                "stage_label": STAGE_LABELS["done"],
                "progress": 1.0,
                "error": None,
            }
            if media.get("title"):
                fields["title"] = str(media["title"])
            if media.get("duration"):
                fields["duration"] = float(media["duration"])
            payload_patch: Dict[str, Any] = {"segment_count": segment_count}
            if stages_dump is not None:
                payload_patch["stages"] = stages_dump
            store.update_job(job_id, fields, payload_patch=payload_patch)
            self._last_stage[job_id] = "done"

    def _fail(self, job_id: str, message: str) -> None:
        text = (message or "").strip() or "处理失败，请重试。"
        with self._lock:
            if job_id in self._cancelled:
                return
            stages = self._stages.get(job_id)
            if stages is not None:
                for stage in stages:
                    if stage.status == "running":
                        stage.status = "error"
                        break
                else:
                    # 没有运行中的阶段：把最后一个未完成阶段标红
                    for stage in reversed(stages):
                        if stage.status in ("pending", "running"):
                            stage.status = "error"
                            break
                stages_dump = _stages_dump(stages)
            else:
                stages_dump = None
            store.update_job(
                job_id,
                {
                    "status": "error",
                    "stage": "error",
                    "stage_label": STAGE_LABELS["error"],
                    "error": text,
                },
                payload_patch={"stages": stages_dump} if stages_dump is not None else None,
            )
            self._last_stage[job_id] = "error"
            logger.warning("作业 %s 失败：%s", job_id, text)
