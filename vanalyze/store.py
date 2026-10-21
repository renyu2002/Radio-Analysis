"""SQLite 持久化层：作业（jobs）与对话消息（messages）。

契约要求（docs/CONTRACT.md §9）：
- 数据库位于 ``data/app.db``，WAL 模式；
- ``jobs`` / ``messages`` 两张表，``Job.stages`` 以 JSON 保存在 ``jobs.payload``；
- 线程安全：每个线程持有自己的 sqlite3 连接（作业线程池 + HTTP 线程池并发访问）。

本模块只负责存取，不做业务状态机（状态机在 ``vanalyze.server.jobs``）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import config
from .models import ChatMessage, Citation, Job, StageState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 连接管理（每线程一个连接）
# ---------------------------------------------------------------------------

_local = threading.local()
_schema_lock = threading.Lock()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    stage       TEXT NOT NULL DEFAULT 'queued',
    stage_label TEXT NOT NULL DEFAULT '排队中',
    progress    REAL NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL DEFAULT 'file',
    title       TEXT NOT NULL DEFAULT '',
    source_url  TEXT,
    duration    REAL NOT NULL DEFAULT 0,
    job_dir     TEXT NOT NULL DEFAULT '',
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    citations  TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_job_id ON messages(job_id, id);
"""

_FIELD_NAMES = {
    "status", "stage", "stage_label", "progress", "source_type", "title",
    "source_url", "duration", "job_dir", "error", "created_at", "updated_at",
    "payload",
}


def db_path() -> Path:
    """当前数据库路径（运行时读取 config，便于测试切换数据目录）。"""
    return Path(config.DB_PATH)


def now_iso() -> str:
    """本地时间 ISO 字符串（带时区偏移，秒精度）。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:  # pragma: no cover - 极少见
        logger.warning("初始化 SQLite PRAGMA 失败", exc_info=True)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）；每个新连接执行一次，兼容测试中数据库文件被重建的情况。"""
    with _schema_lock:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


def get_conn() -> sqlite3.Connection:
    """取得当前线程的数据库连接（必要时建表）。"""
    target = str(db_path())
    conn: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "db_path", None) != target:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass
        conn = None
    if conn is None:
        conn = _connect()
        _local.conn = conn
        _local.db_path = target
        _ensure_schema(conn)
    return conn


def init_db() -> None:
    """建库建表（幂等）。"""
    get_conn()


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# JSON 辅助
# ---------------------------------------------------------------------------


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_payload(raw: Any) -> Dict[str, Any]:
    """解析 payload JSON（字符串或已解析的 dict）。"""
    data = _loads(raw, {})
    return data if isinstance(data, dict) else {}


def row_payload(row: Any) -> Dict[str, Any]:
    """从数据库行中解析 payload JSON。"""
    if row is None:
        return {}
    try:
        raw = row["payload"]
    except (KeyError, IndexError, TypeError):
        return {}
    return parse_payload(raw)


def row_to_job(row: Any) -> Job:
    """数据库行 → ``Job``（不含 has_*/summary 等运行时装饰字段）。"""
    data = dict(row)
    source_type = str(data.get("source_type") or "file")
    if source_type not in ("file", "url", "tab"):
        source_type = "file"
    status = str(data.get("status") or "pending")
    if status not in ("pending", "running", "done", "error"):
        status = "pending"
    stages: List[StageState] = []
    for item in row_payload(row).get("stages") or []:
        try:
            stages.append(StageState.model_validate(item))
        except Exception:  # noqa: BLE001 - 容错旧数据
            continue
    try:
        duration = float(data.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        progress = float(data.get("progress") or 0.0)
    except (TypeError, ValueError):
        progress = 0.0
    return Job(
        id=str(data.get("id") or ""),
        status=status,  # type: ignore[arg-type]
        stage=str(data.get("stage") or "queued"),
        stage_label=str(data.get("stage_label") or "排队中"),
        progress=progress,
        source_type=source_type,  # type: ignore[arg-type]
        title=str(data.get("title") or ""),
        source_url=data.get("source_url"),
        duration=duration,
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        error=data.get("error"),
        stages=stages,
    )


# ---------------------------------------------------------------------------
# 作业
# ---------------------------------------------------------------------------


def create_job(job: Job, payload: Optional[Dict[str, Any]] = None) -> None:
    """插入一条作业记录；payload 中默认写入阶段列表。"""
    p = dict(payload or {})
    p.setdefault("stages", [s.model_dump() for s in job.stages])
    with _tx() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs
                (id, status, stage, stage_label, progress, source_type, title,
                 source_url, duration, job_dir, error, created_at, updated_at, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.id,
                job.status,
                job.stage,
                job.stage_label,
                job.progress,
                job.source_type,
                job.title,
                job.source_url,
                job.duration,
                str(p.get("job_dir") or ""),
                job.error,
                job.created_at or now_iso(),
                job.updated_at or now_iso(),
                _dumps(p),
            ),
        )


def update_job(
    job_id: str,
    fields: Optional[Dict[str, Any]] = None,
    payload_patch: Optional[Dict[str, Any]] = None,
) -> bool:
    """更新作业字段；``payload_patch`` 按键合并进 payload JSON。

    返回 False 表示作业不存在（例如已被删除，此时静默忽略写入）。
    """
    patch = {k: v for k, v in (fields or {}).items() if k in _FIELD_NAMES}
    patch["updated_at"] = patch.get("updated_at") or now_iso()
    with _tx() as conn:
        row = conn.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return False
        if payload_patch:
            payload = row_payload(row)
            for key, value in payload_patch.items():
                payload[key] = value
            patch["payload"] = _dumps(payload)
        columns = ", ".join(f"{name} = ?" for name in patch)
        conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*patch.values(), job_id))
    return True


def get_job_row(job_id: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_job_rows(limit: int = 50) -> List[sqlite3.Row]:
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    conn = get_conn()
    return list(
        conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    )


def delete_job(job_id: str) -> Optional[Dict[str, Any]]:
    """删除作业及其消息，返回被删除的作业行（不存在返回 None）。"""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.execute("DELETE FROM messages WHERE job_id = ?", (job_id,))
    return data


def mark_interrupted(message: str = "服务重启导致中断，请重新分析") -> int:
    """服务启动时：把 pending/running 的残留作业标记为 error。返回处理条数。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, payload FROM jobs WHERE status IN ('pending','running')"
    ).fetchall()
    if not rows:
        return 0
    for row in rows:
        payload = row_payload(row)
        stages = payload.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                if stage.get("status") == "running":
                    stage["status"] = "error"
        update_job(
            str(row["id"]),
            {
                "status": "error",
                "stage": "error",
                "stage_label": "出错",
                "error": message,
            },
            payload_patch={"stages": stages} if isinstance(stages, list) else None,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


def _citation_dicts(citations: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in citations or []:
        if isinstance(item, Citation):
            out.append(item.model_dump())
        elif isinstance(item, dict):
            out.append(dict(item))
        else:
            try:
                out.append(Citation.model_validate(item).model_dump())
            except Exception:  # noqa: BLE001
                continue
    return out


def add_message(
    job_id: str,
    role: str,
    content: str,
    citations: Any = None,
) -> ChatMessage:
    """追加一条对话消息并返回带 id 的 ChatMessage。"""
    created = now_iso()
    cits = _citation_dicts(citations)
    with _tx() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (job_id, role, content, citations, created_at) VALUES (?,?,?,?,?)",
            (job_id, role, content or "", _dumps(cits), created),
        )
        message_id = int(cursor.lastrowid or 0)
    return ChatMessage(
        id=message_id,
        role=role,  # type: ignore[arg-type]
        content=content or "",
        created_at=created,
        citations=[Citation.model_validate(c) for c in cits],
    )


def list_messages(job_id: str, limit: Optional[int] = None) -> List[ChatMessage]:
    """按 id 升序返回消息。"""
    conn = get_conn()
    sql = "SELECT * FROM messages WHERE job_id = ? ORDER BY id ASC"
    params: List[Any] = [job_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    messages: List[ChatMessage] = []
    for row in rows:
        data = dict(row)
        citations: List[Citation] = []
        for item in _loads(data.get("citations"), []):
            try:
                citations.append(Citation.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
        role = str(data.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        messages.append(
            ChatMessage(
                id=int(data.get("id") or 0),
                role=role,  # type: ignore[arg-type]
                content=str(data.get("content") or ""),
                created_at=str(data.get("created_at") or ""),
                citations=citations,
            )
        )
    return messages


def count_messages(job_id: str) -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE job_id = ?", (job_id,)).fetchone()
    return int(row["n"] if row is not None else 0)
