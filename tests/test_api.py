"""API 集成测试（FastAPI TestClient + 本地 DeepSeek stub）。

覆盖 docs/CONTRACT.md §8 的关键端点：
health / settings / upload / url / tab / jobs 列表与详情 / transcript / summary /
ocr / media Range / thumbnail / frames / chat（SSE + 非流式）/ regenerate-summary /
messages / export / 404 / 400 / DELETE / 静态站点 / CORS。

隔离说明
--------
- 数据目录由 conftest 指向临时目录（``VANALYZE_DATA_DIR``），不污染用户数据；
- ``api`` 夹具把 ``JobManager._run_job`` 替换为快速失败桩，上传/URL/Tab 作业
  不会真的跑 ASR / 下载；真实全链路见 ``tests/e2e.py``；
- 需要 core.qa / core.analyze / core.pipeline 的用例在模块未实现时 pytest.skip。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from helpers import (
    SAMPLE_MP4,
    SAMPLE_SPEECH,
    STUB_API_KEY,
    assert_error_response,
    configure_stub_settings,
    default_ocr,
    default_summary,
    default_transcript,
    parse_sse,
    require_module,
    seed_job,
    sse_text,
    wait_until,
)

API = "/api"


def _job_payload(response) -> dict:
    assert response.status_code == 200, f"期望 200，实际 {response.status_code}：{response.text[:400]}"
    data = response.json()
    assert isinstance(data, dict) and "job" in data, f"响应缺少 job：{data!r}"
    return data["job"]


def _wait_job_status(client, job_id: str, statuses, timeout: float = 30.0) -> dict:
    def _probe():
        response = client.get(f"{API}/jobs/{job_id}")
        if response.status_code != 200:
            return None
        job = response.json().get("job") or {}
        return job if job.get("status") in statuses else None

    return wait_until(_probe, timeout=timeout, description=f"作业 {job_id} 进入状态 {statuses}")


# ===========================================================================
# §8.1 health
# ===========================================================================
def test_health(api) -> None:
    response = api.get(f"{API}/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["version"] == "1.0.0"
    assert isinstance(data["deepseek_configured"], bool)
    assert data["deepseek_configured"] is False  # conftest 已清除环境 Key，未配置设置
    assert isinstance(data["port"], int)

    asr = data["asr"]
    assert set(asr) >= {"device", "model", "compute_type", "cuda_available"}
    assert asr["device"] in ("cuda", "cpu")
    assert isinstance(asr["model"], str) and asr["model"]
    assert isinstance(asr["compute_type"], str) and asr["compute_type"]
    assert isinstance(asr["cuda_available"], bool)
    assert asr["cuda_available"] == (asr["device"] == "cuda")


def test_health_reflects_configured_key(api, stub_deepseek) -> None:
    configure_stub_settings(api, stub_deepseek)
    assert api.get(f"{API}/health").json()["deepseek_configured"] is True
    # 清空 Key
    api.put(f"{API}/settings", json={"deepseek_api_key": ""})
    assert api.get(f"{API}/health").json()["deepseek_configured"] is False


# ===========================================================================
# §8.16 / §8.17 settings
# ===========================================================================
def test_settings_get_and_put_masks(api, stub_deepseek) -> None:
    initial = api.get(f"{API}/settings")
    assert initial.status_code == 200
    view = initial.json()["settings"]
    assert view["deepseek_api_key_set"] is False
    assert view["whisper_model"] == "large-v3"
    assert view["data_dir"]

    payload = {
        "deepseek_api_key": STUB_API_KEY,
        "deepseek_base_url": stub_deepseek.base_url,
        "deepseek_model": "deepseek-chat",
        "summary_model": "deepseek-reasoner",
        "whisper_model": "tiny",
        "max_frames": 12,
        "frame_interval": 2.5,
    }
    response = api.put(f"{API}/settings", json=payload)
    assert response.status_code == 200, response.text
    view = response.json()["settings"]
    assert view["deepseek_api_key_set"] is True
    assert view["deepseek_api_key_masked"] != STUB_API_KEY
    assert "…" in view["deepseek_api_key_masked"]
    assert STUB_API_KEY not in response.text  # 明文 Key 绝不回显
    assert view["deepseek_base_url"] == stub_deepseek.base_url
    assert view["summary_model"] == "deepseek-reasoner"
    assert view["whisper_model"] == "tiny"
    assert view["max_frames"] == 12 and view["frame_interval"] == 2.5

    # 未传字段保持不变
    partial = api.put(f"{API}/settings", json={"whisper_device": "cpu"})
    view = partial.json()["settings"]
    assert view["whisper_device"] == "cpu"
    assert view["deepseek_base_url"] == stub_deepseek.base_url
    assert view["deepseek_api_key_set"] is True
    assert api.get(f"{API}/settings").json()["settings"]["whisper_device"] == "cpu"

    # 非法类型 → 统一 400 中文错误
    invalid = api.put(f"{API}/settings", json={"max_frames": "不是数字"})
    detail = assert_error_response(invalid, status=400)
    assert "参数错误" in detail


def test_settings_put_persists_to_settings_json(api, stub_deepseek) -> None:
    from pathlib import Path

    from vanalyze import config

    api.put(f"{API}/settings", json={"deepseek_model": "deepseek-reasoner"})
    settings_file = Path(config.SETTINGS_PATH)
    assert settings_file.is_file()
    text = settings_file.read_text(encoding="utf-8")
    assert "deepseek-reasoner" in text


# ===========================================================================
# §8.2 上传创建作业
# ===========================================================================
def test_upload_creates_job_and_failure(api) -> None:
    fake_video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
    response = api.post(
        f"{API}/jobs/upload",
        files={"file": ("演示视频.mp4", fake_video, "video/mp4")},
        data={"title": "上传测试作业"},
    )
    job = _job_payload(response)
    job_id = job["id"]
    assert job["source_type"] == "file"
    assert job["title"] == "上传测试作业"
    assert job["media_url"] == f"{API}/jobs/{job_id}/media"

    # 后台桩会快速失败，作业应进入 error 且有中文错误消息
    job = _wait_job_status(api, job_id, ("error", "done"), timeout=30)
    assert job["status"] == "error"
    assert job["error"] and any("\u4e00" <= ch <= "\u9fff" for ch in job["error"])

    listing = api.get(f"{API}/jobs?limit=50").json()["jobs"]
    assert any(item["id"] == job_id for item in listing)

    deleted = api.delete(f"{API}/jobs/{job_id}")
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    assert api.get(f"{API}/jobs/{job_id}").status_code == 404


def test_upload_derives_title_from_filename(api) -> None:
    fake_video = b"\x00" * 1024
    response = api.post(
        f"{API}/jobs/upload",
        files={"file": ("我的中文视频.mp4", fake_video, "video/mp4")},
    )
    job = _job_payload(response)
    assert job["title"] == "我的中文视频"
    api.delete(f"{API}/jobs/{job['id']}")


def test_upload_empty_file_rejected(api) -> None:
    response = api.post(
        f"{API}/jobs/upload",
        files={"file": ("empty.mp4", b"", "video/mp4")},
    )
    detail = assert_error_response(response, status=400)
    assert "空" in detail


def test_upload_missing_file_rejected(api) -> None:
    response = api.post(f"{API}/jobs/upload", data={"title": "没有文件"})
    detail = assert_error_response(response, status=400)
    assert "缺少必填字段" in detail or "file" in detail


# ===========================================================================
# §8.3 URL / 标签页 创建作业
# ===========================================================================
def test_create_url_job(api) -> None:
    response = api.post(
        f"{API}/jobs",
        json={"source_type": "url", "url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    )
    job = _job_payload(response)
    assert job["source_type"] == "url"
    assert job["source_url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    api.delete(f"{API}/jobs/{job['id']}")

    bad_scheme = api.post(f"{API}/jobs", json={"source_type": "url", "url": "ftp://example.com/a.mp4"})
    assert_error_response(bad_scheme, status=400, contains="http")

    missing_url = api.post(f"{API}/jobs", json={"source_type": "url"})
    assert_error_response(missing_url, status=400)

    missing_source = api.post(f"{API}/jobs", json={})
    assert_error_response(missing_source, status=400)

    invalid_json = api.post(
        f"{API}/jobs",
        content="这不是 JSON",
        headers={"Content-Type": "application/json"},
    )
    assert_error_response(invalid_json, status=400)


def test_create_tab_job(api) -> None:
    body = {
        "source_type": "tab",
        "page_url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "video_url": "blob:https://www.bilibili.com/stream-abc",
        "title": "当前标签页视频",
        "duration": 123.4,
        "poster": None,
    }
    response = api.post(f"{API}/jobs", json=body)
    job = _job_payload(response)
    assert job["source_type"] == "tab"
    assert job["title"] == "当前标签页视频"
    assert job["duration"] == pytest.approx(123.4)
    assert job["source_url"] == body["page_url"]
    api.delete(f"{API}/jobs/{job['id']}")

    # 缺少 page_url / video_url
    bad = api.post(f"{API}/jobs", json={"source_type": "tab", "title": "只有标题"})
    assert_error_response(bad, status=400)


# ===========================================================================
# §8.4 / §8.5 作业列表与详情
# ===========================================================================
def test_jobs_list_and_detail(api) -> None:
    first = seed_job(title="列表作业一")
    second = seed_job(title="列表作业二", with_summary=False)

    listing = api.get(f"{API}/jobs?limit=50")
    assert listing.status_code == 200
    jobs = listing.json()["jobs"]
    ids = [item["id"] for item in jobs]
    assert first in ids and second in ids
    for item in jobs:
        if item["id"] in (first, second):
            assert item["summary"] is None  # §8.4 列表不含 summary 正文
            assert item["media_url"] == f"{API}/jobs/{item['id']}/media"
            assert item["stages"], "Job.stages 应包含全部阶段"
            assert item["has_transcript"] is True

    limited = api.get(f"{API}/jobs?limit=1").json()["jobs"]
    assert len(limited) == 1

    detail = api.get(f"{API}/jobs/{first}")
    assert detail.status_code == 200
    job = detail.json()["job"]
    assert job["status"] == "done"
    assert job["has_transcript"] and job["has_summary"] and job["has_ocr"]
    assert job["thumbnail_url"] == f"{API}/jobs/{first}/thumbnail"
    assert job["media_url"] == f"{API}/jobs/{first}/media"
    assert job["segment_count"] == 3
    assert job["summary"] is not None
    summary = job["summary"]
    assert summary["one_liner"] and summary["overview"] and summary["chapters"]
    assert job["stages"][-1]["status"] == "done"

    second_detail = api.get(f"{API}/jobs/{second}").json()["job"]
    assert second_detail["has_summary"] is False and second_detail["summary"] is None


def test_job_not_found(api) -> None:
    assert_error_response(api.get(f"{API}/jobs/不存在的作业"), status=404, contains="作业不存在")
    assert_error_response(api.delete(f"{API}/jobs/不存在的作业"), status=404)


# ===========================================================================
# §8.6 / §8.7 / §8.8 转写 / 总结 / 画面文字
# ===========================================================================
def test_transcript_summary_ocr(api) -> None:
    job_id = seed_job(title="产物接口测试")

    transcript_response = api.get(f"{API}/jobs/{job_id}/transcript")
    assert transcript_response.status_code == 200
    transcript = transcript_response.json()["transcript"]
    assert transcript["language"] == "zh"
    assert transcript["duration"] == pytest.approx(11.95)
    assert [segment["id"] for segment in transcript["segments"]] == [1, 2, 3]
    assert "天气" in transcript["text"]
    assert SAMPLE_SPEECH.split("，")[0] in transcript["text"]

    summary_response = api.get(f"{API}/jobs/{job_id}/summary")
    assert summary_response.status_code == 200
    summary = summary_response.json()["summary"]
    assert summary["title"] and summary["one_liner"] and summary["overview"]
    assert summary["chapters"] and summary["key_points"] and summary["keywords"]
    assert summary["generated_by"]

    ocr_response = api.get(f"{API}/jobs/{job_id}/ocr")
    assert ocr_response.status_code == 200
    ocr = ocr_response.json()["ocr"]
    assert len(ocr["frames"]) == 3
    assert "测试画面" in ocr["text"]
    assert all({"time", "text", "score"} <= set(frame) for frame in ocr["frames"])


def test_artifacts_not_ready_404(api) -> None:
    job_id = seed_job(
        title="无产物作业",
        with_transcript=False,
        with_summary=False,
        with_ocr=False,
        with_media=False,
        with_thumbnail=False,
        with_frames=0,
    )
    assert_error_response(api.get(f"{API}/jobs/{job_id}/transcript"), status=404, contains="转写尚未完成")
    assert_error_response(api.get(f"{API}/jobs/{job_id}/summary"), status=404)
    assert_error_response(api.get(f"{API}/jobs/{job_id}/ocr"), status=404)
    assert_error_response(api.get(f"{API}/jobs/{job_id}/media"), status=404)
    assert_error_response(api.get(f"{API}/jobs/{job_id}/thumbnail"), status=404)
    assert_error_response(api.get(f"{API}/jobs/{job_id}/frames/f0001.jpg"), status=404)


# ===========================================================================
# §8.9 媒体流（Range 206） / §8.10 缩略图 / §8.11 关键帧
# ===========================================================================
def test_media_full_and_range(api) -> None:
    job_id = seed_job(title="媒体 Range 测试")
    size = SAMPLE_MP4.stat().st_size
    raw = SAMPLE_MP4.read_bytes()

    full = api.get(f"{API}/jobs/{job_id}/media")
    assert full.status_code == 200
    assert full.headers.get("accept-ranges") == "bytes"
    assert int(full.headers["content-length"]) == size
    assert full.content == raw

    partial = api.get(f"{API}/jobs/{job_id}/media", headers={"Range": "bytes=100-199"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 100-199/{size}"
    assert partial.headers.get("accept-ranges") == "bytes"
    assert len(partial.content) == 100
    assert partial.content == raw[100:200]

    open_ended = api.get(f"{API}/jobs/{job_id}/media", headers={"Range": "bytes=200-"})
    assert open_ended.status_code == 206
    assert open_ended.headers["content-range"] == f"bytes 200-{size - 1}/{size}"
    assert open_ended.content == raw[200:]

    suffix = api.get(f"{API}/jobs/{job_id}/media", headers={"Range": "bytes=-50"})
    assert suffix.status_code == 206
    assert len(suffix.content) == 50
    assert suffix.content == raw[-50:]

    invalid = api.get(f"{API}/jobs/{job_id}/media", headers={"Range": "bytes=999999999-"})
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{size}"


def test_thumbnail_and_frames(api) -> None:
    job_id = seed_job(title="缩略图与关键帧测试", with_frames=3)

    thumbnail = api.get(f"{API}/jobs/{job_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/jpeg")
    assert thumbnail.content[:2] == b"\xff\xd8"

    frame = api.get(f"{API}/jobs/{job_id}/frames/f0001.jpg")
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/jpeg")
    assert frame.content[:2] == b"\xff\xd8"

    assert api.get(f"{API}/jobs/{job_id}/frames/f9999.jpg").status_code == 404
    assert api.get(f"{API}/jobs/{job_id}/frames/not-a-jpg.txt").status_code == 404

    # 路径穿越必须被拒绝
    for path in (
        f"{API}/jobs/{job_id}/frames/..%2F..%2Fsettings.json",
        f"{API}/jobs/{job_id}/frames/%2e%2e%2f%2e%2e%2fapp.db",
        f"{API}/jobs/{job_id}/frames/",
    ):
        response = api.get(path)
        assert response.status_code in (400, 404), f"{path} 未拒绝：{response.status_code}"


# ===========================================================================
# §8.14 问答：无 Key / 参数错误
# ===========================================================================
def test_chat_requires_configured_key(api) -> None:
    job_id = seed_job(title="未配置 Key 问答")
    response = api.post(f"{API}/jobs/{job_id}/chat", json={"question": "视频讲了什么？"})
    detail = assert_error_response(response, status=400)
    assert "API Key" in detail or "尚未配置" in detail

    regenerate = api.post(f"{API}/jobs/{job_id}/regenerate-summary", json={})
    assert_error_response(regenerate, status=400)


def test_chat_validation(api, stub_deepseek) -> None:
    configure_stub_settings(api, stub_deepseek)
    job_id = seed_job(title="问答参数校验")

    empty = api.post(f"{API}/jobs/{job_id}/chat", json={"question": "   "})
    assert_error_response(empty, status=400, contains="问题不能为空")

    no_transcript = seed_job(
        title="未转写作业", with_transcript=False, with_summary=False, with_ocr=False, with_media=False
    )
    not_ready = api.post(f"{API}/jobs/{no_transcript}/chat", json={"question": "视频讲了什么？"})
    assert_error_response(not_ready, status=400)


# ===========================================================================
# §8.14 问答：stub DeepSeek 流式 / 非流式（需要 core.qa 已实现）
# ===========================================================================
def test_chat_stream_false(api, stub_deepseek) -> None:
    require_module("vanalyze.core.qa", ("answer_once",))
    require_module("vanalyze.core.deepseek", ("chat",))
    configure_stub_settings(api, stub_deepseek)
    stub_deepseek.answer = "固定回答：视频讲了语音转文字和天气测试。"
    job_id = seed_job(title="非流式问答")

    response = api.post(
        f"{API}/jobs/{job_id}/chat",
        json={"question": "视频讲了什么？", "stream": False},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["answer"] == stub_deepseek.answer
    assert isinstance(data["citations"], list) and data["citations"]
    for citation in data["citations"]:
        assert {"segment_id", "start", "end", "text"} <= set(citation)
        assert citation["start"] < citation["end"]
    message = data["message"]
    assert message["role"] == "assistant" and message["content"] == data["answer"]
    assert message["id"] > 0

    messages = api.get(f"{API}/jobs/{job_id}/messages").json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "视频讲了什么？"


def test_chat_stream_true_sse(api, stub_deepseek) -> None:
    require_module("vanalyze.core.qa", ("answer_stream",))
    require_module("vanalyze.core.deepseek", ("chat_stream", "DeepSeekError"))
    configure_stub_settings(api, stub_deepseek)
    stub_deepseek.stream_answer_text = "流式固定回答：这是视频分析系统的自检片段。"
    job_id = seed_job(title="流式问答")

    response = api.post(
        f"{API}/jobs/{job_id}/chat",
        json={"question": "音频里说了什么？", "stream": True},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    names = [name for name, _data in events]
    assert "error" not in names, f"SSE 出现 error 事件：{events}"
    assert "delta" in names and "citations" in names and names[-1] == "done"

    delta_text = sse_text(events)
    assert delta_text == stub_deepseek.stream_answer_text

    citation_events = [data for name, data in events if name == "citations"]
    assert len(citation_events) == 1
    items = citation_events[0]["items"]
    assert isinstance(items, list) and items
    for item in items:
        assert {"segment_id", "start", "end", "text"} <= set(item)

    done_events = [data for name, data in events if name == "done"]
    assert len(done_events) == 1
    message_id = done_events[0]["message_id"]
    assert isinstance(message_id, int) and message_id > 0

    # 事件顺序：delta* → citations → done
    first_citation_index = names.index("citations")
    done_index = names.index("done")
    assert all(name == "delta" for name in names[:first_citation_index])
    assert done_index == len(names) - 1

    messages = api.get(f"{API}/jobs/{job_id}/messages").json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["content"] == delta_text
    assert messages[1]["citations"], "助手消息应保存 citations"


def test_chat_sse_error_event_when_upstream_fails(api, stub_deepseek) -> None:
    require_module("vanalyze.core.qa", ("answer_stream",))
    require_module("vanalyze.core.deepseek", ("chat_stream", "DeepSeekError"))
    configure_stub_settings(api, stub_deepseek)
    # 用提示词标记让 stub 对「流式请求」返回 401 → core 立即抛 DeepSeekError，
    # 服务端应在 SSE 中发 error 事件（而不是 HTTP 500）。
    job_id = seed_job(title="流式错误")

    response = api.post(
        f"{API}/jobs/{job_id}/chat",
        json={
            "question": "视频讲了什么？[STUB:STREAM_401]",
            "stream": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _data in events]
    assert "error" in names, f"上游失败应发 error 事件：{events}"
    assert names[-1] == "error"
    error_data = [data for name, data in events if name == "error"][0]
    assert error_data["message"]
    # 错误事件后不应再出现 done
    assert "done" not in names


# ===========================================================================
# §8.12 重新生成总结（需要 core.pipeline.regenerate_summary 已实现）
# ===========================================================================
def test_regenerate_summary(api, stub_deepseek) -> None:
    require_module("vanalyze.core.pipeline", ("regenerate_summary",))
    require_module("vanalyze.core.analyze", ("summarize",))
    configure_stub_settings(api, stub_deepseek)
    job_id = seed_job(title="重新生成总结")

    response = api.post(f"{API}/jobs/{job_id}/regenerate-summary", json={})
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["status"] in ("running", "done")

    job = _wait_job_status(api, job_id, ("done", "error"), timeout=120)
    assert job["status"] == "done", f"重新生成总结失败：{job.get('error')}"
    assert job["has_summary"] is True

    summary = api.get(f"{API}/jobs/{job_id}/summary").json()["summary"]
    assert summary["generated_by"]
    assert summary["chapters"]
    for chapter in summary["chapters"]:
        assert 0 <= chapter["start"] < chapter["end"] <= 11.95 + 0.5


# ===========================================================================
# §8.13 消息列表 / §8.21 导出 / §8.20 静态站点
# ===========================================================================
def test_messages_unknown_job(api) -> None:
    assert_error_response(api.get(f"{API}/jobs/unknown/messages"), status=404)


def test_export_formats(api) -> None:
    job_id = seed_job(title="导出测试视频")

    txt = api.get(f"{API}/jobs/{job_id}/export?format=txt")
    assert txt.status_code == 200
    assert txt.headers["content-type"].startswith("text/plain")
    assert "filename*=UTF-8''" in txt.headers["content-disposition"]
    assert "天气" in txt.text

    srt = api.get(f"{API}/jobs/{job_id}/export?format=srt")
    assert srt.status_code == 200
    assert "00:00:00,000" in srt.text and "-->" in srt.text

    markdown = api.get(f"{API}/jobs/{job_id}/export?format=md")
    assert markdown.status_code == 200
    assert markdown.text.startswith("#")
    assert "章节" in markdown.text

    bad = api.get(f"{API}/jobs/{job_id}/export?format=docx")
    assert_error_response(bad, status=400)

    empty_job = seed_job(title="无转写导出", with_transcript=False)
    assert_error_response(api.get(f"{API}/jobs/{empty_job}/export?format=txt"), status=404)


def test_static_site_and_api_404(api) -> None:
    index = api.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "视频分析" in index.text and "DeepSeek" in index.text
    assert "设置" in index.text

    css = api.get("/style.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    # 前端 SPA 路由回退到 index.html
    spa = api.get("/some/spa/route")
    assert spa.status_code == 200
    assert "视频分析" in spa.text

    missing_api = api.get(f"{API}/not-exists")
    assert_error_response(missing_api, status=404, contains="接口不存在")

    missing_static = api.get("/no-such-file.js")
    assert missing_static.status_code == 404


def test_cors_allows_extension_origin(api) -> None:
    response = api.options(
        f"{API}/health",
        headers={
            "Origin": "chrome-extension://abcdefghijklmnop",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "GET" in response.headers.get("access-control-allow-methods", "")


# ===========================================================================
# §8.18 测试 DeepSeek 连接
# ===========================================================================
def test_settings_test_deepseek(api, stub_deepseek) -> None:
    # 未配置 Key：200 + ok=false（契约允许失败仍返回 200）
    no_key = api.post(f"{API}/settings/test-deepseek")
    assert no_key.status_code == 200
    data = no_key.json()
    assert data["ok"] is False and data["message"]

    configure_stub_settings(api, stub_deepseek)
    configured = api.post(f"{API}/settings/test-deepseek")
    assert configured.status_code == 200
    data = configured.json()
    assert set(data) >= {"ok", "message", "model"}
    assert data["model"] == "deepseek-chat"

    from helpers import module_available

    if module_available("vanalyze.core.deepseek", ("test_connection",)):
        assert data["ok"] is True, data
        assert "连接成功" in data["message"] or data["message"]


# ===========================================================================
# 未捕获异常统一 500（通过一个不存在的作业目录完整性破坏模拟）
# ===========================================================================
def test_error_shape_is_json(api) -> None:
    response = api.get(f"{API}/jobs")
    assert response.status_code == 200
    # 所有 API 错误均为 JSON（不是 HTML）
    for path in (f"{API}/jobs/unknown", f"{API}/jobs/unknown/transcript"):
        error = api.get(path)
        assert error.status_code == 404
        assert error.headers["content-type"].startswith("application/json")
        assert "detail" in error.json()
