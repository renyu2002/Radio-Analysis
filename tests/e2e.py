"""端到端验收（真实服务 + 真实 pipeline + stub DeepSeek）。

验证内容（docs/CONTRACT.md §12）
--------------------------------
1. 起真实服务（127.0.0.1:8798，不占用 8765），数据目录用临时目录隔离；
2. 上传 tests/fixtures/sample.mp4 → 轮询 → done；
3. 真实 ASR：转写文本与 TTS 原文 ≥70% 字符匹配，且包含「语音转文字 / 天气」等关键词；
4. 真实 OCR：画面文字命中「测试画面」；
5. 总结：字段完整、章节时间戳合法（LLM 用本地 stub）；
6. 问答：SSE 事件格式（delta → citations → done）、引用时间戳可解析、消息落库；
7. 媒体 Range 206，可供 <video> seek；
8. （可选，slow）URL 来源：B 站链接，网络环境不支持时 skip。

运行
----
::

    .venv\\Scripts\\python.exe tests\\e2e.py                  # 默认 tiny 模型（快）
    .venv\\Scripts\\python.exe tests\\e2e.py --url            # 额外跑 URL 用例
    $env:E2E_WHISPER_MODEL="large-v3"; .venv\\Scripts\\python.exe tests\\e2e.py

环境变量
--------
- ``E2E_PORT``（默认 8798）
- ``E2E_WHISPER_MODEL``（默认 tiny；真实验收用 large-v3）
- ``E2E_TIMEOUT``（作业处理超时秒数，默认 420）
- ``E2E_URL=1`` 启用 URL 来源用例（慢）
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

# 直接运行 `python tests/e2e.py` 时 sys.path[0] 是 tests/，需要把项目根目录加入才能 import vanalyze
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from helpers import (  # noqa: E402  (需先修正 sys.path)
    ARTIFACTS_DIR,
    ROOT,
    SAMPLE_MP4,
    SAMPLE_SPEECH,
    STUB_API_KEY,
    VENV_PYTHON,
    char_match_ratio,
    contains_any,
    parse_sse,
    pipeline_ready,
    sse_text,
    wait_http_health,
    wait_until,
)

E2E_HOST = "127.0.0.1"
E2E_PORT = int(os.environ.get("E2E_PORT", "8798"))
E2E_BASE_URL = f"http://{E2E_HOST}:{E2E_PORT}"
E2E_WHISPER_MODEL = os.environ.get("E2E_WHISPER_MODEL", "tiny")
E2E_TIMEOUT = float(os.environ.get("E2E_TIMEOUT", "420"))
E2E_ARTIFACTS = ARTIFACTS_DIR / "e2e"
SERVER_LOG = ARTIFACTS_DIR / "e2e_server.log"

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _save_artifact(name: str, data: Any) -> None:
    E2E_ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = E2E_ARTIFACTS / name
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(str(data), encoding="utf-8")


def _log_tail(max_lines: int = 40) -> str:
    try:
        lines = SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "（服务日志不可用）"
    return "\n".join(lines[-max_lines:])


# ---------------------------------------------------------------------------
# 真实服务 fixture（module 级）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live_server(stub_deepseek) -> Tuple[str, Path, subprocess.Popen]:
    """启动真实 uvicorn 服务（子进程），返回 (base_url, 数据目录, 进程)。"""
    if not pipeline_ready():
        pytest.skip("vanalyze.core.pipeline 尚未实现，跳过真实 E2E（等 core-dev 完成后重跑）")
    if not VENV_PYTHON.is_file():
        pytest.skip(f"找不到 venv Python：{VENV_PYTHON}")
    if not SAMPLE_MP4.is_file():
        pytest.skip(f"找不到测试素材：{SAMPLE_MP4}")
    if not _port_free(E2E_HOST, E2E_PORT):
        raise RuntimeError(
            f"端口 {E2E_PORT} 已被占用，无法启动 E2E 服务。请检查是否有残留进程："
            f"Get-NetTCPConnection -LocalPort {E2E_PORT}"
        )

    data_dir = Path(tempfile.mkdtemp(prefix="vanalyze_e2e_"))
    env = os.environ.copy()
    env.update(
        {
            "VANALYZE_DATA_DIR": str(data_dir),
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "DEEPSEEK_API_KEY": STUB_API_KEY,
            "DEEPSEEK_BASE_URL": stub_deepseek.base_url,
            "VANALYZE_WHISPER_MODEL": E2E_WHISPER_MODEL,
            "VANALYZE_WHISPER_DEVICE": os.environ.get("E2E_WHISPER_DEVICE", "auto"),
            "VANALYZE_FRAME_INTERVAL": os.environ.get("E2E_FRAME_INTERVAL", "2.0"),
        }
    )
    # 不继承外部真实 Key 相关变量以外的覆盖项；显式清掉用户的设置文件影响（临时目录本来就是空的）
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    command = [str(VENV_PYTHON), "-m", "vanalyze", "--host", E2E_HOST, "--port", str(E2E_PORT)]
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # pragma: no cover - Windows only
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    try:
        health = wait_http_health(E2E_BASE_URL, timeout=120)
        print(f"\n[E2E] 服务已就绪：{E2E_BASE_URL} health={json.dumps(health, ensure_ascii=False)}")
        assert health.get("ok") is True
        yield E2E_BASE_URL, data_dir, process
    finally:
        try:
            process.terminate()
            process.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        log_handle.close()
        if os.environ.get("E2E_KEEP_DATA", "") not in ("1", "true", "yes"):
            shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def http(live_server):
    """httpx 客户端（module 级）。"""
    import httpx

    base_url, data_dir, process = live_server
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        yield client


@pytest.fixture(scope="module")
def file_job(http) -> Dict[str, Any]:
    """上传 sample.mp4 并等待处理完成，返回作业详情（module 内复用）。"""
    import httpx

    assert SAMPLE_MP4.is_file()
    with SAMPLE_MP4.open("rb") as handle:
        response = http.post(
            "/api/jobs/upload",
            files={"file": ("sample.mp4", handle, "video/mp4")},
            data={"title": "E2E 自检视频"},
            timeout=120.0,
        )
    assert response.status_code == 200, f"上传失败：{response.status_code} {response.text[:400]}"
    created = response.json()["job"]
    job_id = created["id"]
    print(f"\n[E2E] 已上传 sample.mp4 → 作业 {job_id}，等待处理（模型 {E2E_WHISPER_MODEL}）…")

    last_stage = {"stage": "", "progress": -1.0}
    poll_start = time.monotonic()

    def _probe() -> Optional[Dict[str, Any]]:
        detail = http.get(f"/api/jobs/{job_id}")
        if detail.status_code != 200:
            return None
        job = detail.json()["job"]
        stage = str(job.get("stage") or "")
        progress = float(job.get("progress") or 0.0)
        if stage != last_stage["stage"] or progress - last_stage["progress"] >= 0.2:
            elapsed = time.monotonic() - poll_start
            print(f"[E2E] {elapsed:6.1f}s  stage={stage:<14} progress={progress:5.1%}  {job.get('stage_label')}")
            last_stage.update(stage=stage, progress=progress)
        return job if job.get("status") in ("done", "error") else None

    try:
        job = wait_until(_probe, timeout=E2E_TIMEOUT, interval=0.5, description="作业处理完成")
    except AssertionError:
        pytest.fail(f"作业 {job_id} 处理超时（{E2E_TIMEOUT:.0f}s）。服务日志尾部：\n{_log_tail()}")
    if job.get("status") != "done":
        pytest.fail(f"作业 {job_id} 处理失败：{job.get('error')}\n服务日志尾部：\n{_log_tail()}")
    print(f"[E2E] 作业完成：{job_id}（{time.monotonic() - poll_start:.1f}s）")
    return job


# ---------------------------------------------------------------------------
# 1) ASR + OCR + summary 全链路
# ---------------------------------------------------------------------------
def test_file_job_completed(http, file_job) -> None:
    assert file_job["status"] == "done"
    assert file_job["has_transcript"] is True, "作业完成后应有 transcript.json"
    assert file_job["has_summary"] is True, "作业完成后应有 summary.json（无 Key 时降级版也算）"
    assert file_job["duration"] == pytest.approx(11.95, abs=1.0)
    assert file_job["media_url"] == f"/api/jobs/{file_job['id']}/media"
    assert file_job["segment_count"] > 0
    _save_artifact("job.json", file_job)


def test_transcript_real_asr(http, file_job) -> None:
    response = http.get(f"/api/jobs/{file_job['id']}/transcript")
    assert response.status_code == 200, response.text
    transcript = response.json()["transcript"]
    _save_artifact("transcript.json", transcript)

    text = transcript.get("text") or ""
    assert text.strip(), "真实 ASR 转写文本为空"
    assert transcript["segments"], "真实 ASR 应返回 segments"
    assert [segment["id"] for segment in transcript["segments"]] == list(
        range(1, len(transcript["segments"]) + 1)
    )
    # 契约 §12.2：与 TTS 原文 ≥70% 字符匹配
    ratio = char_match_ratio(SAMPLE_SPEECH, text)
    print(f"\n[E2E] ASR 字符匹配率：{ratio:.1%}\n[E2E] 转写：{text}")
    assert ratio >= 0.70, f"ASR 字符匹配率 {ratio:.1%} < 70%（转写：{text!r}）"
    assert contains_any(text, ["语音转文字", "天气"]), f"转写缺少关键词（语音转文字/天气）：{text!r}"
    for segment in transcript["segments"]:
        assert 0 <= segment["start"] < segment["end"] <= 13.0


def test_ocr_real_frames(http, file_job) -> None:
    response = http.get(f"/api/jobs/{file_job['id']}/ocr")
    assert response.status_code == 200, response.text
    ocr = response.json()["ocr"]
    _save_artifact("ocr.json", ocr)

    assert ocr["frames"], "OCR 结果为空（画面文字识别失败或抽帧失败）"
    ocr_text = ocr.get("text") or ""
    frame_text = "\n".join(str(frame.get("text") or "") for frame in ocr["frames"])
    combined = ocr_text + "\n" + frame_text
    print(f"\n[E2E] OCR：{ocr_text[:500]}")
    assert contains_any(combined, ["测试画面"]), f"OCR 未命中「测试画面」：{combined[:800]!r}"
    assert any(frame.get("frame") for frame in ocr["frames"]), "OCR 帧应带 frames/ 文件名"
    for frame in ocr["frames"]:
        assert 0 <= float(frame["time"]) <= 13.0


def test_summary_fields_complete(http, file_job) -> None:
    response = http.get(f"/api/jobs/{file_job['id']}/summary")
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    _save_artifact("summary.json", summary)

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
        "generated_by",
    ):
        assert field in summary, f"summary 缺字段：{field}"
    assert summary["title"] and summary["one_liner"] and summary["overview"]
    assert summary["generated_by"], "使用 stub DeepSeek 时 generated_by 应为模型名"
    assert summary["topics"] and summary["key_points"] and summary["keywords"]
    assert summary["chapters"], "summary 应包含章节"
    for chapter in summary["chapters"]:
        assert 0 <= chapter["start"] < chapter["end"] <= 13.0, f"章节时间戳非法：{chapter}"
        assert chapter["title"]


# ---------------------------------------------------------------------------
# 2) 媒体 Range（<video> seek）
# ---------------------------------------------------------------------------
def test_media_range_seek(http, file_job) -> None:
    full = http.get(f"/api/jobs/{file_job['id']}/media")
    assert full.status_code == 200
    size = int(full.headers["content-length"])
    assert size == SAMPLE_MP4.stat().st_size
    assert full.headers.get("accept-ranges") == "bytes"

    partial = http.get(
        f"/api/jobs/{file_job['id']}/media",
        headers={"Range": "bytes=1000-2999"},
    )
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 1000-2999/{size}"
    assert len(partial.content) == 2000


# ---------------------------------------------------------------------------
# 3) 问答（stub DeepSeek：SSE 事件 + citations + 落库）
# ---------------------------------------------------------------------------
def test_chat_stream_sse(http, file_job, stub_deepseek) -> None:
    from helpers import module_available

    if not module_available("vanalyze.core.qa", ("answer_stream",)):
        pytest.skip("core.qa 尚未实现，跳过真实问答 E2E")
    stub_deepseek.stream_answer_text = "根据视频转写，这段自检视频主要测试了语音转文字与画面文字识别。"
    job_id = file_job["id"]

    response = http.post(
        f"/api/jobs/{job_id}/chat",
        json={"question": "视频里提到了什么测试？", "stream": True},
        timeout=120.0,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _data in events]
    print(f"\n[E2E] SSE 事件：{names}")
    assert "error" not in names, f"SSE 出现 error：{events}"
    assert "delta" in names and "citations" in names and names[-1] == "done"

    answer = sse_text(events)
    assert answer.strip()
    citations = [data for name, data in events if name == "citations"][0]["items"]
    assert citations, "问答应返回 citations 引用"
    for item in citations:
        assert 0 <= float(item["start"]) < float(item["end"]) <= 13.0
    done = [data for name, data in events if name == "done"][0]
    assert isinstance(done["message_id"], int) and done["message_id"] > 0

    messages = http.get(f"/api/jobs/{job_id}/messages").json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"].strip()


# ---------------------------------------------------------------------------
# 4) 设置 / 健康检查（真实服务）
# ---------------------------------------------------------------------------
def test_live_health_and_settings(http) -> None:
    health = http.get("/api/health").json()
    assert health["ok"] is True
    assert health["version"] == "1.0.0"
    assert health["deepseek_configured"] is True
    print(f"\n[E2E] health={json.dumps(health, ensure_ascii=False)}")

    settings = http.get("/api/settings").json()["settings"]
    assert settings["deepseek_api_key_set"] is True
    assert STUB_API_KEY not in json.dumps(settings)
    assert settings["deepseek_base_url"].startswith("http://127.0.0.1:")


# ---------------------------------------------------------------------------
# 5) 可选：URL 来源（B 站短链；默认跳过，E2E_URL=1 启用）
# ---------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("E2E_URL", "") not in ("1", "true", "yes"),
    reason="URL 来源 E2E 需联网且依赖 B 站可访问性，设置 E2E_URL=1 启用",
)
def test_url_source(http) -> None:
    # 默认用 3 分半左右的稳定公开视频（34 分钟的长视频会超过合理的验收等待时间）
    url = os.environ.get("E2E_URL_TARGET", "https://www.bilibili.com/video/BV1GJ411x7h7")
    url_timeout = float(os.environ.get("E2E_URL_TIMEOUT", "600"))
    response = http.post("/api/jobs", json={"source_type": "url", "url": url}, timeout=60.0)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    job_id = job["id"]
    assert job["source_type"] == "url"
    assert job["source_url"] == url
    assert any(stage["key"] == "downloading" for stage in job["stages"]), "url 作业应有 downloading 阶段"
    print(f"\n[E2E] URL 作业：{job_id} ← {url}")

    last_stage = {"stage": ""}
    started = time.monotonic()

    def _probe():
        detail = http.get(f"/api/jobs/{job_id}")
        if detail.status_code != 200:
            return None
        job = detail.json()["job"]
        stage = str(job.get("stage") or "")
        if stage != last_stage["stage"]:
            elapsed = time.monotonic() - started
            print(
                f"[E2E] {elapsed:6.1f}s  stage={stage:<12} progress={float(job.get('progress') or 0):5.1%}  "
                f"{job.get('stage_label')}"
            )
            last_stage["stage"] = stage
        return job if job.get("status") in ("done", "error") else None

    try:
        job = wait_until(_probe, timeout=url_timeout, interval=1.0, description="URL 作业完成")
    except AssertionError:
        pytest.skip(f"URL 作业在 {url_timeout:.0f}s 内未完成（当前网络/视频长度限制，最后阶段：{last_stage['stage']}）")
    if job.get("status") == "error":
        pytest.skip(f"URL 解析失败（当前网络环境不可用）：{job.get('error')}")
    assert job["status"] == "done"
    assert job["title"]
    assert float(job["duration"]) > 30

    transcript = http.get(f"/api/jobs/{job_id}/transcript")
    assert transcript.status_code == 200
    data = transcript.json()["transcript"]
    assert data["text"].strip(), "URL 作业转写为空"
    assert len(data["segments"]) > 0
    _save_artifact("url_transcript.json", data)

    media = http.get(f"/api/jobs/{job_id}/media", headers={"Range": "bytes=0-99"})
    assert media.status_code == 206
    print(f"[E2E] URL 作业完成：{job['title']!r}，{len(data['segments'])} 段转写")


# ===========================================================================
# 独立运行入口：python tests/e2e.py [--url]
# ===========================================================================
if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--url" in argv:
        os.environ["E2E_URL"] = "1"
        argv.remove("--url")
    sys.exit(pytest.main([str(Path(__file__).resolve()), "-v", "-s", *argv]))
