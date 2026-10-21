"""测试公共工具：种子作业、SSE 解析、环境探测、关键词匹配等。

该模块不依赖 pytest 的导入（pytest 仅在 skip 辅助函数内部按需 import），
因此 e2e / ui_smoke 等独立脚本也可以复用。
"""

from __future__ import annotations

import difflib
import importlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
ARTIFACTS_DIR = TESTS_DIR / ".artifacts"
SAMPLE_MP4 = FIXTURES_DIR / "sample.mp4"
SLIDE_PNGS = [FIXTURES_DIR / f"slide{i}.png" for i in (1, 2, 3)]
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: sample.mp4 中 TTS 朗读的原文（见 tests/fixtures/make_fixture.py）
SAMPLE_SPEECH = (
    "今天天气很好，我们来测试语音转文字功能。"
    "这是视频分析系统的自检音频，请确认转写结果是否准确。"
)

#: sample.mp4 画面上的中文字样
SAMPLE_SLIDE_TEXTS = [
    "第一章  测试画面",
    "视频分析系统自检",
    "画面文字 OCR 测试",
    "识别画面中的中文字幕",
    "语音转文字测试",
    "共 12 秒，含中文普通话语音",
]

#: E2E / stub 通用假 Key（stub 接受任意 Bearer）
STUB_API_KEY = "sk-stub-test-key-1234567890"


# ---------------------------------------------------------------------------
# 文本处理 / 匹配
# ---------------------------------------------------------------------------
def normalize_cn(text: Any) -> str:
    """去掉空白与标点，只保留文字字符（便于中文相似度比较）。"""
    return re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE)


def char_match_ratio(expected: str, actual: str) -> float:
    """按字符序列计算相似度 0..1（验收 §12.2「≥70% 字符匹配」）。"""
    expected_norm = normalize_cn(expected)
    actual_norm = normalize_cn(actual)
    if not expected_norm:
        return 0.0
    if not actual_norm:
        return 0.0
    return difflib.SequenceMatcher(None, expected_norm, actual_norm).ratio()


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    """文本是否包含任意关键词（忽略空白差异）。"""
    normalized = normalize_cn(text)
    return any(normalize_cn(keyword) in normalized for keyword in keywords if keyword)


def find_items(items: Iterable[Any], keyword: str) -> List[Any]:
    """在一组对象/字符串中找包含关键词的项（对象支持 dict 的 text/content 字段）。"""
    needle = normalize_cn(keyword)
    found: List[Any] = []
    for item in items or []:
        if isinstance(item, str):
            haystack = normalize_cn(item)
        elif isinstance(item, dict):
            haystack = normalize_cn(
                " ".join(str(item.get(key) or "") for key in ("text", "content", "title", "summary"))
            )
        else:
            haystack = normalize_cn(getattr(item, "text", "") or "")
        if needle and needle in haystack:
            found.append(item)
    return found


# ---------------------------------------------------------------------------
# 文件 / JSON
# ---------------------------------------------------------------------------
def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def make_test_jpeg(path: Path, text: str = "测试画面", size: Tuple[int, int] = (320, 180)) -> None:
    """生成一张带中文文字的 JPEG（缩略图 / 关键帧测试用）。"""
    from PIL import Image, ImageDraw, ImageFont

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, (14, 18, 28))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0], 8], fill=(77, 107, 254))
    font = None
    for candidate in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ):
        try:
            if Path(candidate).is_file():
                font = ImageFont.truetype(candidate, max(12, size[1] // 8))
                break
        except OSError:
            continue
    if font is None:  # pragma: no cover - 没有中文字体时退化为默认字体
        font = ImageFont.load_default()
    draw.text((16, size[1] // 2 - size[1] // 12), str(text or "测试画面"), fill=(240, 244, 255), font=font)
    image.save(path, format="JPEG", quality=82)


# ---------------------------------------------------------------------------
# 默认产物数据（与 tests/fixtures/sample.mp4 内容一致）
# ---------------------------------------------------------------------------
def default_transcript(duration: float = 11.95) -> Dict[str, Any]:
    segments = [
        (1, 0.0, 3.8, "今天天气很好，我们来测试语音转文字功能。"),
        (2, 3.8, 7.6, "这是视频分析系统的自检音频，"),
        (3, 7.6, 11.6, "请确认转写结果是否准确。"),
    ]
    return {
        "language": "zh",
        "duration": duration,
        "segments": [
            {"id": seg_id, "start": start, "end": end, "text": text}
            for seg_id, start, end, text in segments
        ],
        "text": "\n".join(text for _id, _start, _end, text in segments),
    }


def default_summary() -> Dict[str, Any]:
    """导入 stub 的固定 Summary（避免两处维护不一致）。"""
    from stub_deepseek import summary_payload

    payload = summary_payload()
    payload["generated_by"] = "deepseek-chat"
    payload["created_at"] = "2026-01-01T00:00:00+08:00"
    return payload


def default_ocr() -> Dict[str, Any]:
    frames = [
        {"time": 0.0, "text": "第一章  测试画面 / 视频分析系统自检", "frame": "f0001.jpg", "score": 0.96},
        {"time": 4.0, "text": "画面文字 OCR 测试 / 识别画面中的中文字幕", "frame": "f0002.jpg", "score": 0.94},
        {"time": 8.0, "text": "语音转文字测试 / 共 12 秒，含中文普通话语音", "frame": "f0003.jpg", "score": 0.95},
    ]
    return {
        "frames": frames,
        "text": "\n".join(f"[00:{index * 4:02d}] {item['text']}" for index, item in enumerate(frames)),
    }


def default_media_info(title: str = "测试视频（sample.mp4）", duration: float = 11.95) -> Dict[str, Any]:
    from vanalyze.models import MediaInfo

    return MediaInfo(
        path=str(SAMPLE_MP4),
        source_type="file",
        title=title,
        duration=duration,
        ext=".mp4",
        local_input=True,
    ).model_dump()


# ---------------------------------------------------------------------------
# 种子作业（绕过 pipeline，直接构造 DB + job_dir 产物，供 API 测试使用）
# ---------------------------------------------------------------------------
def seed_job(
    *,
    job_id: Optional[str] = None,
    status: str = "done",
    source_type: str = "file",
    title: str = "测试视频（sample.mp4）",
    duration: float = 11.95,
    with_transcript: bool = True,
    with_summary: bool = True,
    with_ocr: bool = True,
    with_media: bool = True,
    with_thumbnail: bool = True,
    with_frames: int = 3,
    transcript: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
    ocr: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    source_url: Optional[str] = None,
) -> str:
    """在临时数据目录中创建一个已完成（或指定状态）的作业，返回 job_id。

    直接写 DB 与 ``data/jobs/<job_id>/`` 产物，不触发真实 pipeline。
    """
    from vanalyze import config, store
    from vanalyze.models import Job, StageState
    from vanalyze.server.jobs import STAGE_LABELS, stage_plan

    job_id = job_id or uuid.uuid4().hex[:12]
    job_dir = Path(config.JOBS_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    if with_media:
        write_json(job_dir / "media.json", default_media_info(title=title, duration=duration))
    if with_transcript:
        write_json(job_dir / "transcript.json", transcript or default_transcript(duration))
    if with_summary:
        write_json(job_dir / "summary.json", summary or default_summary())
    if with_ocr:
        write_json(job_dir / "ocr.json", ocr or default_ocr())
    if with_thumbnail:
        make_test_jpeg(job_dir / "thumbnail.jpg", "测试画面")
    for index in range(int(with_frames or 0)):
        make_test_jpeg(job_dir / "frames" / f"f{index + 1:04d}.jpg", f"关键帧 {index + 1}")

    plan = stage_plan(source_type)
    if status == "done":
        stages = [StageState(key=key, label=STAGE_LABELS.get(key, key), status="done", progress=1.0) for key in plan]
        stage, stage_label, progress = "done", STAGE_LABELS["done"], 1.0
    elif status == "error":
        stages = [
            StageState(
                key=key,
                label=STAGE_LABELS.get(key, key),
                status="done" if index == 0 else ("error" if index == 1 else "pending"),
                progress=1.0 if index == 0 else 0.0,
            )
            for index, key in enumerate(plan)
        ]
        stage, stage_label, progress = "error", STAGE_LABELS["error"], 0.5
    else:
        stages = [
            StageState(
                key=key,
                label=STAGE_LABELS.get(key, key),
                status="running" if index == 0 else "pending",
                progress=0.0,
            )
            for index, key in enumerate(plan)
        ]
        stage, stage_label, progress = "queued", STAGE_LABELS["queued"], 0.0

    created = store.now_iso()
    job = Job(
        id=job_id,
        status=status,  # type: ignore[arg-type]
        stage=stage,
        stage_label=stage_label,
        progress=progress,
        source_type=source_type,  # type: ignore[arg-type]
        title=title,
        source_url=source_url,
        duration=duration,
        created_at=created,
        updated_at=created,
        error=error,
        stages=stages,
    )
    segment_count = len((transcript or default_transcript(duration)).get("segments") or []) if with_transcript else 0
    payload: Dict[str, Any] = {
        "job_dir": str(job_dir),
        "source": {"source_type": source_type, "file_path": str(SAMPLE_MP4)},
        "stages": [item.model_dump() for item in stages],
        "segment_count": segment_count,
    }
    store.create_job(job, payload)
    return job_id


def purge_runtime_data() -> None:
    """清空临时数据目录中的作业 / 上传文件 / 设置，保证测试互不影响。"""
    from vanalyze import config, store
    from vanalyze.settings import Settings, save_settings

    try:
        store.init_db()
        for row in store.list_job_rows(500):
            store.delete_job(str(row["id"]))
    except Exception:  # noqa: BLE001 - 清理失败不应影响测试结果
        pass
    for directory in (Path(config.JOBS_DIR), Path(config.DATA_DIR) / "uploads"):
        try:
            if directory.exists():
                for child in directory.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink(missing_ok=True)
                        except OSError:
                            pass
        except OSError:
            pass
    try:
        save_settings(Settings())
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# API 测试辅助
# ---------------------------------------------------------------------------
def configure_stub_settings(client: Any, stub: Any, **overrides: Any) -> Dict[str, Any]:
    """把服务端设置指向本地 stub DeepSeek（通过 PUT /api/settings）。"""
    payload: Dict[str, Any] = {
        "deepseek_api_key": STUB_API_KEY,
        "deepseek_base_url": getattr(stub, "base_url", str(stub)),
        "deepseek_model": "deepseek-chat",
        "summary_model": "",
        "whisper_model": "tiny",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "max_frames": 6,
        "frame_interval": 2.0,
        "vision_enabled": False,
    }
    payload.update(overrides)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, (
        f"PUT /api/settings 失败：{response.status_code} {response.text[:300]}"
    )
    return response.json()


def assert_error_response(
    response: Any,
    *,
    status: Optional[int] = None,
    contains: Optional[str] = None,
) -> str:
    """断言统一错误格式 ``{"detail": "中文错误消息"}``，返回 detail。"""
    if status is not None:
        assert response.status_code == status, (
            f"期望 HTTP {status}，实际 {response.status_code}：{response.text[:300]}"
        )
    data = response.json()
    assert isinstance(data, dict) and "detail" in data, f"错误响应不是统一格式：{data!r}"
    detail = str(data["detail"])
    assert detail, "错误 detail 不能为空"
    assert re.search(r"[\u3400-\u9fff]", detail), f"错误消息应为中文：{detail!r}"
    if contains is not None:
        assert contains in detail, f"错误消息缺少「{contains}」：{detail!r}"
    return detail


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------
def module_available(module_name: str, attrs: Iterable[str] = ()) -> bool:
    """模块可导入且（可选）具备指定属性时返回 True。"""
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - 依赖缺失 / 语法错误均视为未就绪
        return False
    return all(hasattr(module, attr) for attr in attrs)


def require_module(module_name: str, attrs: Iterable[str] = (), reason: str = "") -> Any:
    """模块未实现时 ``pytest.skip``（而不是 collection error）。"""
    import pytest

    attrs = tuple(attrs)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{module_name} 尚未实现/不可导入：{exc}")
    missing = [attr for attr in attrs if not hasattr(module, attr)]
    if missing:
        pytest.skip(f"{module_name} 缺少接口 {missing}（尚未实现）" + (f"：{reason}" if reason else ""))
    return module


def pipeline_ready() -> bool:
    """core.pipeline 是否已实现（E2E 前置条件）。"""
    return module_available("vanalyze.core.pipeline", ("run_pipeline",))


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 30.0,
    interval: float = 0.2,
    description: str = "条件",
) -> Any:
    """轮询直到 predicate 返回真值；超时抛 AssertionError。"""
    deadline = time.monotonic() + float(timeout)
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待{description}超时（{timeout:.0f}s），最后结果：{last!r}")


# ---------------------------------------------------------------------------
# SSE 解析（契约 §8.14）
# ---------------------------------------------------------------------------
def parse_sse(text: str) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    """把 SSE 响应文本解析为 [(event, data)]，兼容 CRLF 与多行 data。"""
    events: List[Tuple[Optional[str], Dict[str, Any]]] = []
    for block in re.split(r"\r?\n\r?\n", str(text or "")):
        block = block.strip("\r\n")
        if not block:
            continue
        event: Optional[str] = None
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if event is None and not data_lines:
            continue
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            events.append((event, {"__done__": True}))
            continue
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"__raw__": raw}
        if not isinstance(payload, dict):
            payload = {"__value__": payload}
        events.append((event, payload))
    return events


def sse_text(events: Iterable[Tuple[Optional[str], Dict[str, Any]]]) -> str:
    """拼接 delta 事件的文本。"""
    return "".join(str(data.get("text") or "") for event, data in events if event == "delta")


# ---------------------------------------------------------------------------
# HTTP 轮询（e2e / ui_smoke 用，httpx 在 venv 中已安装）
# ---------------------------------------------------------------------------
def wait_http_health(base_url: str, *, timeout: float = 90.0) -> Dict[str, Any]:
    """等待真实服务的 /api/health 可用（启动 uvicorn 需要一点时间）。"""
    import httpx

    deadline = time.monotonic() + float(timeout)
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=3.0)
            if response.status_code == 200:
                return response.json()
        except Exception as exc:  # noqa: BLE001 - 服务尚未就绪
            last_error = exc
        time.sleep(0.4)
    raise RuntimeError(f"等待服务 {base_url} 就绪超时（{timeout:.0f}s）：{last_error}")


def chrome_path() -> Optional[Path]:
    """定位本机 Chrome（UI 冒烟测试用）。"""
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
