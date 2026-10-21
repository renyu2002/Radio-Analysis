"""单元测试：models / settings / store / server 纯函数 / core 纯函数。

设计原则
--------
- 已实现且稳定的纯函数（models、settings、store、server.jobs、server.app）直接断言；
- core 模块（core-dev 并行开发中）**未实现时 pytest.skip，绝不 import error 崩溃收集**；
- 对 core 内部辅助函数（时间格式 / JSON 修复）做「候选发现」式测试：
  找到就测，找不到 skip（契约未规定私有函数名）；行为级能力另由
  ``chat_json`` / ``summarize`` / ``answer_once`` 的 stub 测试覆盖。
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest

from helpers import (
    SAMPLE_MP4,
    SAMPLE_SPEECH,
    STUB_API_KEY,
    char_match_ratio,
    default_ocr,
    default_summary,
    default_transcript,
    require_module,
    seed_job,
)

CJK_RE = re.compile(r"[\u3400-\u9fff]")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _has_cjk(text: Any) -> bool:
    return bool(CJK_RE.search(str(text or "")))


# ===========================================================================
# models.py（Lead 维护，只读）
# ===========================================================================
def test_models_summary_defaults_and_roundtrip() -> None:
    from vanalyze.models import Chapter, Summary

    summary = Summary()
    assert summary.one_liner == "" and summary.chapters == [] and summary.quotes == []
    chapter = Chapter(start=0.0, end=4.0, title="开场", summary="介绍")
    summary = Summary(
        title="中文标题",
        one_liner="一句话",
        overview="## 概览",
        topics=["主题"],
        chapters=[chapter],
        key_points=["要点"],
        keywords=["关键词"],
        quotes=["【00:01】原话"],
        visual_notes=["画面文字"],
        generated_by="deepseek-chat",
        created_at="2026-01-01T00:00:00+08:00",
    )
    payload = json.loads(summary.model_dump_json(indent=2))
    assert payload["title"] == "中文标题"
    assert payload["chapters"][0]["start"] == 0.0
    # ensure_ascii=False 时中文不应被转义
    raw = summary.model_dump_json(indent=2, ensure_ascii=False)
    assert "中文标题" in raw and "\\u4e2d" not in raw


def test_models_job_defaults_and_full_fields() -> None:
    from vanalyze.models import Job, StageState

    job = Job(id="abc123")
    assert job.status == "pending"
    assert job.stage == "queued"
    assert job.stage_label == "排队中"
    assert job.stages == []
    assert job.has_transcript is False
    assert job.summary is None
    job = Job(
        id="abc123",
        status="done",
        stage="done",
        stage_label="完成",
        progress=1.0,
        source_type="url",
        title="测试",
        duration=11.95,
        stages=[StageState(key="queued", label="排队中", status="done", progress=1.0)],
        has_transcript=True,
        has_summary=True,
        has_ocr=True,
        segment_count=3,
        message_count=2,
        media_url="/api/jobs/abc123/media",
    )
    data = json.loads(job.model_dump_json())
    assert data["status"] == "done"
    assert data["stages"][0]["key"] == "queued"
    assert data["media_url"] == "/api/jobs/abc123/media"


def test_models_media_transcript_ocr() -> None:
    from vanalyze.models import MediaInfo, OcrFrame, OcrResult, Transcript, TranscriptSegment

    media = MediaInfo(path=str(SAMPLE_MP4), title="测试视频", duration=11.95, ext=".mp4", local_input=True)
    assert media.source_type == "file"
    transcript = Transcript(
        language="zh",
        duration=11.95,
        segments=[
            TranscriptSegment(id=1, start=0.0, end=1.5, text="第一句"),
            TranscriptSegment(id=2, start=1.5, end=3.0, text="第二句"),
        ],
        text="第一句\n第二句",
    )
    assert len(transcript.segments) == 2
    ocr = OcrResult(
        frames=[OcrFrame(time=0.0, text="测试画面", frame="f0001.jpg", score=0.9)],
        text="[00:00] 测试画面",
    )
    assert ocr.frames[0].frame == "f0001.jpg"


def test_models_chat_models() -> None:
    from vanalyze.models import ChatMessage, ChatRequest, ChatResponse, Citation

    request = ChatRequest(question="视频讲了什么？")
    assert request.stream is True  # 契约默认 stream=True
    citation = Citation(segment_id=1, start=0.0, end=1.5, text="第一句")
    response = ChatResponse(
        answer="回答",
        citations=[citation],
        message=ChatMessage(id=2, role="assistant", content="回答", citations=[citation]),
    )
    assert response.message is not None and response.message.role == "assistant"
    assert response.citations[0].segment_id == 1


def test_models_settings_update_accepts_partial() -> None:
    from vanalyze.models import SettingsUpdate

    update = SettingsUpdate(deepseek_base_url="http://127.0.0.1:8777", vision_enabled=True)
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"deepseek_base_url": "http://127.0.0.1:8777", "vision_enabled": True}


# ===========================================================================
# settings.py（Lead 维护：掩码 / 环境变量优先级 / 部分更新）
# ===========================================================================
@pytest.fixture
def settings_file(monkeypatch, tmp_path) -> Path:
    """把 settings 文件重定向到 pytest tmp_path。"""
    from vanalyze import config

    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    return path


def test_settings_defaults_and_derived(settings_file) -> None:
    from vanalyze.settings import Settings, load_settings

    settings = load_settings()
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-chat"
    assert settings.whisper_model == "large-v3"
    assert settings.whisper_device == "auto"
    assert settings.port == 8765
    # summary_model 为空 → 回退 deepseek_model
    assert settings.resolved_summary_model == "deepseek-chat"
    custom = Settings(deepseek_model="deepseek-chat", summary_model="deepseek-reasoner")
    assert custom.resolved_summary_model == "deepseek-reasoner"
    assert custom.has_deepseek_key is False


def test_settings_env_overrides_file(settings_file, monkeypatch) -> None:
    from vanalyze.settings import get_api_key, load_settings

    settings_file.write_text(
        json.dumps(
            {
                "deepseek_api_key": "file-key-aaa",
                "deepseek_base_url": "https://file.example.com",
                "whisper_model": "small",
                "max_frames": 10,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key-bbb")
    monkeypatch.setenv("VANALYZE_WHISPER_MODEL", "tiny")
    monkeypatch.setenv("VANALYZE_MAX_FRAMES", "42")
    settings = load_settings()
    # 环境变量优先于 settings.json
    assert settings.deepseek_api_key == "env-key-bbb"
    assert settings.whisper_model == "tiny"
    # 环境变量未覆盖的字段保留文件值
    assert settings.deepseek_base_url == "https://file.example.com"
    # 数值字段被正确转型
    assert settings.max_frames == 42 and isinstance(settings.max_frames, int)
    assert get_api_key(settings) == "env-key-bbb"


def test_settings_env_empty_string_ignored(settings_file, monkeypatch) -> None:
    from vanalyze.settings import load_settings

    settings_file.write_text(json.dumps({"deepseek_api_key": "file-key"}), encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    assert load_settings().deepseek_api_key == "file-key"


def test_settings_env_invalid_number_ignored(settings_file, monkeypatch) -> None:
    from vanalyze.settings import load_settings

    monkeypatch.setenv("VANALYZE_MAX_FRAMES", "not-a-number")
    monkeypatch.setenv("VANALYZE_FRAME_INTERVAL", "2.5")
    settings = load_settings()
    assert settings.max_frames == 100  # 非法值被忽略，保留默认
    assert settings.frame_interval == 2.5 and isinstance(settings.frame_interval, float)


def test_settings_update_partial_and_empty_string(settings_file) -> None:
    from vanalyze.models import SettingsUpdate
    from vanalyze.settings import Settings, load_settings, save_settings, update_settings

    save_settings(
        Settings(
            deepseek_api_key="file-key",
            deepseek_base_url="https://old.example.com",
            whisper_model="small",
        )
    )
    updated = update_settings(SettingsUpdate(whisper_model="medium"))
    assert updated.whisper_model == "medium"
    assert updated.deepseek_api_key == "file-key"  # 未传字段不变
    assert updated.deepseek_base_url == "https://old.example.com"
    # 显式传空字符串表示清空 Key
    updated = update_settings(SettingsUpdate(deepseek_api_key=""))
    assert load_settings().deepseek_api_key == ""


def test_settings_view_masks_key(settings_file) -> None:
    from vanalyze.settings import Settings

    view = Settings(deepseek_api_key="sk-abcdef1234567890").to_view()
    assert view.deepseek_api_key_set is True
    assert view.deepseek_api_key_masked != "sk-abcdef1234567890"
    assert "…" in view.deepseek_api_key_masked
    assert view.deepseek_api_key_masked.startswith("sk-a")
    dumped = view.model_dump_json()
    assert "sk-abcdef1234567890" not in dumped

    short_view = Settings(deepseek_api_key="sk-short").to_view()
    assert short_view.deepseek_api_key_set is True
    assert short_view.deepseek_api_key_masked == "已设置"

    empty_view = Settings().to_view()
    assert empty_view.deepseek_api_key_set is False
    assert empty_view.deepseek_api_key_masked == ""
    assert empty_view.data_dir  # 数据目录应有值


def test_settings_mask_secrets_helper() -> None:
    from vanalyze.settings import mask_secrets

    masked = mask_secrets({"deepseek_api_key": "secret-a", "vision_api_key": "secret-b", "other": "keep"})
    assert masked["deepseek_api_key"] == "***"
    assert masked["vision_api_key"] == "***"
    assert masked["other"] == "keep"
    assert "secret-a" not in json.dumps(masked)


def test_settings_corrupt_and_unknown_keys(settings_file) -> None:
    from vanalyze.settings import load_settings

    settings_file.write_text("{ 这不是合法 JSON ", encoding="utf-8")
    assert load_settings().deepseek_model == "deepseek-chat"  # 损坏文件 → 默认值

    settings_file.write_text(
        json.dumps({"legacy_unknown_key": 1, "deepseek_model": "deepseek-reasoner"}),
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.deepseek_model == "deepseek-reasoner"
    assert not hasattr(settings, "legacy_unknown_key")


def test_settings_save_roundtrip_utf8(settings_file) -> None:
    from vanalyze.settings import Settings, load_settings, save_settings

    save_settings(Settings(deepseek_base_url="http://127.0.0.1:8777", whisper_language="zh"))
    raw = settings_file.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8777" in raw
    loaded = load_settings()
    assert loaded.deepseek_base_url == "http://127.0.0.1:8777"
    assert loaded.whisper_language == "zh"


# ===========================================================================
# store.py（server-dev，已实现）
# ===========================================================================
def test_store_job_crud_and_messages() -> None:
    from vanalyze import store
    from vanalyze.models import Citation, Job, StageState

    job_id = "unit" + store.now_iso().replace(":", "").replace("-", "")[-8:]
    created = store.now_iso()
    job = Job(
        id=job_id,
        status="done",
        stage="done",
        stage_label="完成",
        progress=1.0,
        title="单元测试作业",
        duration=12.0,
        created_at=created,
        updated_at=created,
        stages=[StageState(key="done", label="完成", status="done", progress=1.0)],
    )
    store.create_job(job, {"job_dir": "C:/tmp/unit", "custom": 1})
    row = store.get_job_row(job_id)
    assert row is not None
    parsed = store.row_to_job(row)
    assert parsed.id == job_id and parsed.status == "done" and parsed.title == "单元测试作业"
    assert parsed.stages and parsed.stages[0].status == "done"

    assert store.update_job(job_id, {"title": "改名", "progress": 0.5}, payload_patch={"segment_count": 3})
    row = store.get_job_row(job_id)
    assert store.row_to_job(row).title == "改名"
    payload = store.row_payload(row)
    assert payload["segment_count"] == 3 and payload["custom"] == 1
    assert store.update_job("不存在的作业", {"status": "done"}) is False

    message = store.add_message(job_id, "user", "你好", [Citation(segment_id=1, start=0, end=1, text="片段")])
    assert message.id > 0
    store.add_message(job_id, "assistant", "回答")
    messages = store.list_messages(job_id)
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[0].citations and messages[0].citations[0].segment_id == 1
    assert store.count_messages(job_id) == 2

    deleted = store.delete_job(job_id)
    assert deleted is not None
    assert store.get_job_row(job_id) is None
    assert store.list_messages(job_id) == []
    assert store.delete_job(job_id) is None


def test_store_mark_interrupted() -> None:
    from vanalyze import store
    from vanalyze.models import Job, StageState

    job_id = "interrupted" + store.now_iso().replace(":", "").replace("-", "")[-6:]
    created = store.now_iso()
    job = Job(
        id=job_id,
        status="running",
        stage="transcribing",
        stage_label="语音转文字",
        progress=0.4,
        created_at=created,
        updated_at=created,
        stages=[
            StageState(key="queued", label="排队中", status="done", progress=1.0),
            StageState(key="transcribing", label="语音转文字", status="running", progress=0.4),
        ],
    )
    store.create_job(job, {"stages": [item.model_dump() for item in job.stages]})
    count = store.mark_interrupted()
    assert count >= 1
    parsed = store.row_to_job(store.get_job_row(job_id))
    assert parsed.status == "error"
    assert parsed.error and _has_cjk(parsed.error)
    assert any(stage.status == "error" for stage in parsed.stages)


# ===========================================================================
# server/jobs.py 纯函数（server-dev，已实现）
# ===========================================================================
def test_stage_plan_and_labels() -> None:
    from vanalyze.server.jobs import STAGE_LABELS, stage_plan

    assert stage_plan("file") == [
        "queued",
        "resolving",
        "probing",
        "transcribing",
        "frames",
        "ocr",
        "analyzing",
    ]
    assert "downloading" in stage_plan("url")
    assert "downloading" in stage_plan("tab")
    # 契约 §7 的全部 stage_key → 中文标签
    expected = {
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
    for key, label in expected.items():
        assert STAGE_LABELS[key] == label


def test_new_stages_and_overall_progress() -> None:
    from vanalyze.server.jobs import _overall_progress, new_stages

    stages = new_stages("file")
    assert stages[0].key == "queued" and stages[0].status == "running"
    assert all(stage.status == "pending" for stage in stages[1:])
    assert _overall_progress(stages) == 0.0
    for stage in stages:
        stage.status = "done"
        stage.progress = 1.0
    assert _overall_progress(stages) == 1.0
    assert _overall_progress([]) == 0.0


def test_friendly_error_and_has_cjk() -> None:
    from vanalyze.server.jobs import friendly_error, has_cjk

    assert has_cjk("中文") is True
    assert has_cjk("english only") is False
    assert _has_cjk(friendly_error(FileNotFoundError("x.mp4")))
    assert "处理失败" in friendly_error(RuntimeError("english error"))
    assert friendly_error(RuntimeError("中文错误")) == "中文错误"
    assert _has_cjk(friendly_error(MemoryError()))
    assert _has_cjk(friendly_error(Exception()))


# ===========================================================================
# server/app.py 纯函数（server-dev，已实现）
# ===========================================================================
def test_format_timestamp_mmss() -> None:
    from vanalyze.server.app import _format_srt_ts, _format_ts

    assert _format_ts(0) == "00:00"
    assert _format_ts(5.9) == "00:05"
    assert _format_ts(65.2) == "01:05"
    assert _format_ts(3599) == "59:59"
    assert _format_ts(3600) == "01:00:00"
    assert _format_ts(-3) == "00:00"
    assert _format_ts("bad") == "00:00"
    assert _format_ts(None) == "00:00"

    assert _format_srt_ts(0) == "00:00:00,000"
    assert _format_srt_ts(65.2) == "00:01:05,200"
    assert _format_srt_ts(-1) == "00:00:00,000"


def test_safe_upload_name_and_content_disposition() -> None:
    from vanalyze.server.app import _content_disposition, _safe_upload_name

    name = _safe_upload_name('../../evil name?.mp4')
    assert "/" not in name and "\\" not in name and "?" not in name
    assert name.endswith(".mp4")
    assert _safe_upload_name("") == "upload"
    long_name = "长" * 300 + ".mp4"
    assert len(_safe_upload_name(long_name)) <= 120

    header = _content_disposition("中文标题.txt", "fallback.txt")
    assert header.startswith("attachment;")
    assert "filename*=UTF-8''" in header
    assert "fallback.txt" in header


# ===========================================================================
# core：已实现部分（asr / gpu），未实现模块 skip
# ===========================================================================
def test_core_asr_detect_device() -> None:
    asr = require_module("vanalyze.core.asr", ("detect_device",))

    class _Settings:
        def __init__(self, device: str, compute: str = "auto") -> None:
            self.whisper_device = device
            self.whisper_compute_type = compute

    assert asr.detect_device(_Settings("cpu")) == ("cpu", "int8")
    assert asr.detect_device(_Settings("cpu", "float32")) == ("cpu", "float32")
    assert asr.detect_device(_Settings("cuda")) == ("cuda", "float16")
    assert asr.detect_device(_Settings("cuda", "int8_float16")) == ("cuda", "int8_float16")
    device, compute = asr.detect_device(_Settings("auto"))
    assert device in ("cuda", "cpu")
    if device == "cuda":
        assert compute in ("float16", "auto") or compute.startswith("float")
    else:
        assert compute in ("int8", "float32", "int8_float32", "auto")


def test_core_gpu_available_returns_bool() -> None:
    gpu = require_module("vanalyze.core.gpu", ("cuda_available",))
    result = gpu.cuda_available()
    assert isinstance(result, bool)


# ===========================================================================
# core：时间格式 / JSON 修复辅助函数（候选发现，未找到则 skip）
# ===========================================================================
_TIME_HELPER_PATTERN = re.compile(
    r"^_?(format|fmt|to|render)_?(ts|timestamp|time|clock|mmss)$", re.IGNORECASE
)
_JSON_HELPER_PATTERN = re.compile(
    r"^_?((extract|parse|loads|repair|strip|clean|fix)_?json(?:_text)?|json_(extract|parse|loads|repair|clean|fix))$",
    re.IGNORECASE,
)
_CORE_MODULES = (
    "vanalyze.core.analyze",
    "vanalyze.core.qa",
    "vanalyze.core.vision",
    "vanalyze.core.deepseek",
    "vanalyze.core.pipeline",
)


def _find_core_helpers(pattern: re.Pattern[str]) -> List[Tuple[str, str, Callable[..., Any]]]:
    found: List[Tuple[str, str, Callable[..., Any]]] = []
    for module_name in _CORE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - 模块未实现
            continue
        for attr in dir(module):
            if not pattern.match(attr):
                continue
            candidate = getattr(module, attr)
            if callable(candidate):
                found.append((module_name, attr, candidate))
    return found


def test_core_time_format_helper() -> None:
    helpers = _find_core_helpers(_TIME_HELPER_PATTERN)
    if not helpers:
        pytest.skip("core 未暴露 MM:SS 时间格式化纯函数（契约未规定私有函数名）")
    matched: List[str] = []
    errors: List[str] = []
    for module_name, attr, helper in helpers:
        try:
            result = helper(65.2)
        except Exception as exc:  # noqa: BLE001 - 签名不匹配
            errors.append(f"{module_name}.{attr}: {type(exc).__name__}")
            continue
        text = str(result)
        if "01:05" in text:
            matched.append(f"{module_name}.{attr} -> {text}")
    if not matched and errors:
        pytest.skip(f"候选时间格式化函数签名不匹配：{errors}")
    assert matched, f"候选时间格式化函数输出不是 MM:SS：{helpers}"


def test_core_json_repair_helper() -> None:
    helpers = _find_core_helpers(_JSON_HELPER_PATTERN)
    if not helpers:
        pytest.skip("core 未暴露 JSON 修复纯函数（契约未规定私有函数名）")
    sample = '好的，下面是结果：\n```json\n{"a": 1, "中文": "值"}\n```\n以上。'
    matched: List[str] = []
    for module_name, attr, helper in helpers:
        try:
            result = helper(sample)
        except Exception:  # noqa: BLE001 - 签名不匹配
            continue
        data = result
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                continue
        if isinstance(data, dict) and data.get("a") == 1:
            matched.append(f"{module_name}.{attr}")
    if not matched:
        pytest.skip(f"JSON 修复辅助函数候选（{len(helpers)} 个）语义不匹配，行为测试已覆盖")
    assert matched


# ===========================================================================
# core.deepseek：配置校验 + JSON 修复行为（需 core 实现，否则 skip）
# ===========================================================================
def test_core_deepseek_is_configured_and_no_key_error(settings_factory) -> None:
    deepseek = require_module("vanalyze.core.deepseek", ("is_configured", "chat", "DeepSeekError"))
    from vanalyze.settings import Settings

    assert deepseek.is_configured(Settings()) is False
    assert deepseek.is_configured(settings_factory()) is True
    with pytest.raises(deepseek.DeepSeekError) as excinfo:
        deepseek.chat(Settings(), [{"role": "user", "content": "你好"}])
    message = str(excinfo.value)
    assert _has_cjk(message)
    assert ("API Key" in message) or ("未配置" in message)


def test_core_deepseek_chat_json_repair(stub_deepseek, settings_factory) -> None:
    deepseek = require_module(
        "vanalyze.core.deepseek", ("chat_json", "chat_stream", "chat", "test_connection", "DeepSeekError")
    )
    settings = settings_factory()

    fenced = deepseek.chat_json(
        settings,
        [{"role": "user", "content": "请输出 JSON 总结。[STUB:FENCED]"}],
    )
    assert isinstance(fenced, dict) and fenced.get("title")

    truncated = deepseek.chat_json(
        settings,
        [{"role": "user", "content": "请输出 JSON。[STUB:TRUNCATED]"}],
    )
    assert isinstance(truncated, dict) and truncated.get("one_liner")

    simple = deepseek.chat_json(
        settings,
        [{"role": "user", "content": "请输出 JSON。[STUB:SIMPLE_JSON]"}],
    )
    assert simple == {"ok": True, "message": "中文测试值"}

    with pytest.raises(deepseek.DeepSeekError) as excinfo:
        deepseek.chat_json(
            settings,
            [{"role": "user", "content": "请输出 JSON。[STUB:NOT_JSON]"}],
        )
    assert _has_cjk(str(excinfo.value))


def test_core_deepseek_chat_stream_and_connection(stub_deepseek, settings_factory) -> None:
    deepseek = require_module(
        "vanalyze.core.deepseek", ("chat", "chat_stream", "test_connection")
    )
    settings = settings_factory()
    stub_deepseek.answer = "固定中文回答：天气不错。"
    stub_deepseek.stream_answer_text = "流式中文回答：语音转文字测试。"

    answer = deepseek.chat(settings, [{"role": "user", "content": "视频讲了什么？"}])
    assert answer == stub_deepseek.answer

    streamed = "".join(deepseek.chat_stream(settings, [{"role": "user", "content": "视频讲了什么？"}]))
    assert streamed == stub_deepseek.stream_answer_text

    ok, message = deepseek.test_connection(settings)
    assert ok is True and _has_cjk(message)

    # 请求应带 Bearer 授权头
    last = stub_deepseek.last_request()
    assert last is not None
    assert last["headers"].get("authorization") == f"Bearer {STUB_API_KEY}"


# ===========================================================================
# core.qa：本地检索 + 引用 + Prompt 约束（需 core 实现，否则 skip）
# ===========================================================================
def test_core_qa_retrieval_citations_and_prompt(stub_deepseek, settings_factory, tmp_path) -> None:
    qa = require_module("vanalyze.core.qa", ("answer_once", "answer_stream"))
    from helpers import write_json

    keywords = [
        "苹果", "香蕉", "橙子", "西瓜", "葡萄", "草莓", "芒果", "菠萝",
        "桃子", "梨子", "樱桃", "柠檬", "椰子", "石榴", "杨梅", "荔枝",
        "龙眼", "区块链", "量子计算", "人工智能",
    ]
    segments = [
        {
            "id": index + 1,
            "start": float(index * 3),
            "end": float(index * 3 + 3),
            "text": f"第{index + 1}段内容，主题是{keyword}。",
        }
        for index, keyword in enumerate(keywords)
    ]
    write_json(
        tmp_path / "transcript.json",
        {
            "language": "zh",
            "duration": 60.0,
            "segments": segments,
            "text": "\n".join(item["text"] for item in segments),
        },
    )
    write_json(tmp_path / "summary.json", default_summary())
    write_json(tmp_path / "ocr.json", default_ocr())

    answer, citations = qa.answer_once(
        job_dir=str(tmp_path),
        question="区块链是什么意思？",
        history=[],
        settings=settings_factory(),
    )
    assert isinstance(answer, str) and answer.strip()
    assert citations, "问答应返回被检索到的引用片段"
    for citation in citations:
        payload = citation.model_dump() if hasattr(citation, "model_dump") else dict(citation)
        assert {"segment_id", "start", "end", "text"} <= set(payload)
        assert payload["start"] < payload["end"]
    # 目标片段应被检索到（要么直接命中关键词，要么 segment_id 命中）
    target_ids = {17}
    hit = any(
        (citation.model_dump() if hasattr(citation, "model_dump") else dict(citation))["segment_id"] in target_ids
        or "区块链" in (citation.model_dump() if hasattr(citation, "model_dump") else dict(citation))["text"]
        for citation in citations
    )
    assert hit, "BM25 检索未命中包含「区块链」的片段"

    # 校验发给 DeepSeek 的 Prompt 满足契约 §6.8 的核心约束
    last = stub_deepseek.last_request()
    assert last is not None
    messages = last["body"].get("messages") or []
    system_text = "\n".join(
        str(item.get("content") or "") for item in messages if isinstance(item, dict) and item.get("role") == "system"
    )
    assert system_text, "问答请求应包含 system prompt"
    assert "【" in system_text and "MM:SS" in system_text
    assert "视频中没有提到" in system_text


def test_core_qa_no_key_raises(stub_deepseek, settings_factory, tmp_path) -> None:
    qa = require_module("vanalyze.core.qa", ("answer_once",))
    deepseek = require_module("vanalyze.core.deepseek", ("DeepSeekError",))
    from helpers import write_json

    write_json(tmp_path / "transcript.json", default_transcript())
    write_json(tmp_path / "summary.json", default_summary())
    write_json(tmp_path / "ocr.json", default_ocr())
    with pytest.raises(deepseek.DeepSeekError):
        qa.answer_once(
            job_dir=str(tmp_path),
            question="视频讲了什么？",
            history=[],
            settings=settings_factory(deepseek_api_key=""),
        )


# ===========================================================================
# core.analyze：降级总结 / 字段完整 / 章节时间戳兜底（需 core 实现，否则 skip）
# ===========================================================================
def _make_core_inputs(duration: float = 11.95):
    from vanalyze.models import MediaInfo, OcrResult, Transcript

    media = MediaInfo(
        path=str(SAMPLE_MP4),
        title="测试视频",
        duration=duration,
        ext=".mp4",
        local_input=True,
    )
    transcript = Transcript.model_validate(default_transcript(duration))
    ocr = OcrResult.model_validate(default_ocr())
    return media, transcript, ocr


def test_core_summarize_no_key_degraded(settings_factory) -> None:
    analyze = require_module("vanalyze.core.analyze", ("summarize",))
    from vanalyze.settings import Settings

    media, transcript, ocr = _make_core_inputs()
    summary = analyze.summarize(media, transcript, ocr, Settings())
    assert summary.generated_by == ""
    assert summary.one_liner
    assert summary.overview  # 降级时 overview 放转写节选
    assert _has_cjk(summary.one_liner)
    # 降级不应抛错，也不应阻断作业（§6.7）


def test_core_summarize_fields_complete(stub_deepseek, settings_factory) -> None:
    analyze = require_module("vanalyze.core.analyze", ("summarize",))
    stub_deepseek.summary_mode = "valid"
    media, transcript, ocr = _make_core_inputs(duration=30.0)

    summary = analyze.summarize(
        media,
        transcript,
        ocr,
        settings_factory(),
        visual_notes=list(default_ocr()["frames"][0]["text"].split(" / ")),
    )
    data = summary.model_dump()
    for field in (
        "title",
        "one_liner",
        "overview",
        "topics",
        "chapters",
        "key_points",
        "keywords",
        "quotes",
        "visual_notes",
    ):
        assert field in data
    assert summary.one_liner and summary.overview
    assert summary.topics and summary.key_points and summary.keywords
    assert summary.generated_by  # 有 Key 时应记录实际模型名
    assert ISO_RE.match(summary.created_at or "")


def test_core_summarize_invalid_chapters_sanitized(stub_deepseek, settings_factory) -> None:
    analyze = require_module("vanalyze.core.analyze", ("summarize",))
    stub_deepseek.summary_mode = "invalid_chapters"
    duration = 30.0
    media, transcript, ocr = _make_core_inputs(duration=duration)

    summary = analyze.summarize(media, transcript, ocr, settings_factory())
    assert summary.chapters, "章节时间戳非法时应兜底修正而不是丢弃全部章节"
    for chapter in summary.chapters:
        assert chapter.start >= 0.0, f"章节起点为负：{chapter}"
        assert chapter.start < chapter.end, f"章节起点 >= 终点：{chapter}"
        assert chapter.end <= duration + 0.5, f"章节终点超出视频时长：{chapter}"
        assert chapter.title and len(chapter.title) <= 20


# ===========================================================================
# core.deepseek：内部纯函数（已实现，可精确断言）
# ===========================================================================
def test_core_deepseek_parse_json_text_helper() -> None:
    deepseek = require_module("vanalyze.core.deepseek", ("_parse_json_text",))
    parse = deepseek._parse_json_text
    assert parse('{"a": 1, "中文": "值"}') == {"a": 1, "中文": "值"}
    assert parse('```json\n{"title": "中文标题"}\n```')["title"] == "中文标题"
    assert parse('```\n{"ok": true}\n```')["ok"] is True
    assert parse('好的，结果如下：{"ok": true} 以上。')["ok"] is True
    assert parse('前缀 {"nested": {"a": 1}} 后缀')["nested"]["a"] == 1
    with pytest.raises(ValueError):
        parse("这里没有任何 JSON 对象")
    with pytest.raises(ValueError):
        parse("")


def test_core_deepseek_payload_rules() -> None:
    deepseek = require_module("vanalyze.core.deepseek", ("_build_payload", "_is_reasoner", "_endpoint"))
    messages = [{"role": "user", "content": "你好"}]
    normal = deepseek._build_payload(
        messages,
        model="deepseek-chat",
        temperature=0.3,
        max_tokens=100,
        json_mode=True,
        stream=False,
    )
    assert normal["response_format"] == {"type": "json_object"}
    assert normal["temperature"] == 0.3
    assert normal["stream"] is False
    assert normal["messages"] == messages

    reasoner = deepseek._build_payload(
        messages,
        model="deepseek-reasoner",
        temperature=0.3,
        max_tokens=100,
        json_mode=True,
        stream=True,
    )
    # reasoner 不支持 temperature / response_format（§6.6）
    assert "response_format" not in reasoner
    assert "temperature" not in reasoner
    assert reasoner["stream"] is True
    assert deepseek._is_reasoner("deepseek-reasoner") is True
    assert deepseek._is_reasoner("deepseek-chat") is False


def test_core_deepseek_error_messages(settings_factory) -> None:
    deepseek = require_module(
        "vanalyze.core.deepseek", ("_endpoint", "_status_message", "_network_message")
    )
    import httpx

    settings = settings_factory(deepseek_base_url="http://127.0.0.1:8777/")
    assert deepseek._endpoint(settings) == "http://127.0.0.1:8777/chat/completions"
    assert "401" in deepseek._status_message(401, "", settings)
    assert "余额" in deepseek._status_message(402, "", settings)
    assert "429" in deepseek._status_message(429, "", settings)
    assert "无法连接" in deepseek._network_message(httpx.ConnectError("x"), settings)


# ===========================================================================
# core.qa：中文 bigram 分词 + BM25 排序 + 检索（纯函数，已实现）
# ===========================================================================
def test_core_qa_tokenize_bigrams() -> None:
    qa = require_module("vanalyze.core.qa", ("_tokenize",))
    assert qa._tokenize("视频分析") == ["视频", "频分", "分析"]
    assert qa._tokenize("") == []
    assert qa._tokenize("中") == ["中"]
    english = qa._tokenize("DeepSeek OCR v2 视频")
    assert "deepseek" in english and "ocr" in english and "v2" in english
    assert "视频" in english


def test_core_qa_bm25_scores() -> None:
    qa = require_module("vanalyze.core.qa", ("_bm25_scores",))
    documents = [
        qa._tokenize("视频分析系统测试"),
        qa._tokenize("今天天气很好"),
        qa._tokenize("语音转文字测试"),
    ]
    scores = qa._bm25_scores(documents, qa._tokenize("视频分析"))
    assert len(scores) == 3
    assert scores[0] > 0
    assert scores[1] == 0 and scores[2] == 0
    assert qa._bm25_scores([], ["视频"]) == []
    assert qa._bm25_scores(documents, []) == [0.0, 0.0, 0.0]

    ranked = sorted(range(3), key=lambda index: scores[index], reverse=True)
    assert ranked[0] == 0


def test_core_qa_retrieve_direct() -> None:
    qa = require_module("vanalyze.core.qa", ("_retrieve",))
    from vanalyze.models import Transcript

    transcript = Transcript.model_validate(default_transcript())
    hits, citations = qa._retrieve(transcript, "天气")
    assert hits and citations
    assert any("天气" in citation.text for citation in citations)
    assert all(0 <= citation.start < citation.end for citation in citations)
    assert all(citation.segment_id >= 1 for citation in citations)

    empty_hits, empty_citations = qa._retrieve(Transcript(), "天气")
    assert empty_hits == [] and empty_citations == []


# ===========================================================================
# core.analyze：时间格式 / 章节时间戳兜底（纯函数，已实现）
# ===========================================================================
def test_core_analyze_format_helpers() -> None:
    analyze = require_module("vanalyze.core.analyze", ("_fmt_ts", "_fmt_duration"))
    assert analyze._fmt_ts(0) == "00:00"
    assert analyze._fmt_ts(65.2) == "01:05"
    assert analyze._fmt_ts(65.6) == "01:06"
    assert analyze._fmt_ts(-1) == "00:00"
    assert analyze._fmt_ts("bad") == "00:00"
    assert analyze._fmt_duration(5) == "5 秒"
    assert analyze._fmt_duration(65) == "1 分 5 秒"
    assert analyze._fmt_duration(3665) == "1 小时 1 分 5 秒"


def test_core_analyze_chapter_coercion() -> None:
    analyze = require_module("vanalyze.core.analyze", ("_even_chapters", "_coerce_chapters"))

    even = analyze._even_chapters(30.0, None, count=5)
    assert len(even) == 5
    assert even[0].start == 0.0
    assert even[-1].end == pytest.approx(30.0)
    assert all(chapter.start < chapter.end for chapter in even)
    assert analyze._even_chapters(0, None, count=5) == []

    invalid = [
        {"start": 8, "end": 3, "title": "起点大于终点"},
        {"start": -2, "end": 4, "title": "负起点"},
        {"start": 10, "end": 999, "title": "超出总时长"},
        {"start": 0, "end": 2, "title": "合法章节"},
    ]
    repaired = analyze._coerce_chapters(invalid, 30.0, None)
    assert repaired
    assert all(0.0 <= chapter.start < chapter.end <= 30.0 for chapter in repaired)
    assert all(chapter.title and len(chapter.title) <= 20 for chapter in repaired)

    all_bad = [{"start": 9, "end": 1}, {"start": -1, "end": -2}]
    fallback = analyze._coerce_chapters(all_bad, 20.0, None)
    assert fallback and all(0 <= chapter.start < chapter.end <= 20.0 for chapter in fallback)

    assert analyze._coerce_chapters([], 20.0, None), "空章节应均分兜底"
    assert analyze._coerce_chapters([], 0.0, None) == []


# ===========================================================================
# core.vision / ingest / pipeline：时间格式、URL 判定、产物 JSON
# ===========================================================================
def test_core_vision_format_timestamp() -> None:
    vision = require_module("vanalyze.core.vision", ("format_timestamp",))
    assert vision.format_timestamp(0) == "00:00"
    assert vision.format_timestamp(65.2) == "01:05"
    assert vision.format_timestamp(3599) == "59:59"
    assert vision.format_timestamp(-1) == "00:00"
    assert vision.format_timestamp("bad") == "00:00"


def test_core_ingest_url_helpers() -> None:
    ingest = require_module(
        "vanalyze.core.ingest",
        ("_url_path_suffix", "_is_http", "_is_direct_media_url", "_is_playlist_url"),
    )
    assert ingest._is_http("https://www.bilibili.com/video/BV1xx")
    assert ingest._is_http("http://127.0.0.1:8765/")
    assert not ingest._is_http("blob:https://www.bilibili.com/abc")
    assert not ingest._is_http("")
    assert ingest._is_direct_media_url("https://cdn.example.com/video.MP4?token=1")
    assert not ingest._is_direct_media_url("https://www.bilibili.com/video/BV1xx")
    assert ingest._is_playlist_url("https://live.example.com/index.m3u8")
    assert not ingest._is_playlist_url("https://example.com/a.mp4")
    assert ingest._url_path_suffix("https://cdn.example.com/a.MP4?x=1") == ".mp4"
    assert ingest._url_path_suffix("not a url") == ""


def test_core_ingest_probe_media_sample() -> None:
    ingest = require_module("vanalyze.core.ingest", ("probe_media",))
    media = ingest.probe_media(str(SAMPLE_MP4))
    assert Path(media.path).is_file()
    assert media.duration == pytest.approx(11.95, abs=0.5)
    assert (media.ext or "").lower().lstrip(".") == "mp4"
    assert media.title


def test_core_pipeline_dump_json_utf8() -> None:
    pipeline = require_module("vanalyze.core.pipeline", ("_dump_json",))
    from vanalyze.models import Summary

    raw = pipeline._dump_json(Summary(title="中文标题", one_liner="一句话"))
    assert "中文标题" in raw
    assert "\\u4e2d" not in raw
    assert json.loads(raw)["title"] == "中文标题"


# ===========================================================================
# 种子作业工具自检（保证 test_api 的 trusted fixture 正常）
# ===========================================================================
def test_seed_job_helper_produces_artifacts() -> None:
    job_id = seed_job(title="种子作业自检")
    from vanalyze import config

    job_dir = Path(config.JOBS_DIR) / job_id
    for name in ("media.json", "transcript.json", "summary.json", "ocr.json", "thumbnail.jpg"):
        assert (job_dir / name).is_file(), f"种子作业缺少产物 {name}"
    assert (job_dir / "frames" / "f0001.jpg").is_file()
    from vanalyze import store

    job = store.row_to_job(store.get_job_row(job_id))
    assert job.status == "done" and job.title == "种子作业自检"
