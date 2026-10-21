"""真实联调冒烟脚本（server-dev 自用，不参与服务运行）。

一条命令跑完整链路：

    # 自己拉起一个临时服务（默认 8799），跑完自动关停
    .\\.venv\\Scripts\\python.exe -m vanalyze.server._smoke

    # 也可以对已启动的服务执行（例如 8765 上的正式实例）
    .\\.venv\\Scripts\\python.exe -m vanalyze.server._smoke --base http://127.0.0.1:8765

默认流程：启动服务 → 等待 /api/health → 上传 tests/fixtures/sample.mp4 →
轮询阶段进度 → done 后拉 transcript / summary / ocr / thumbnail / export →
Range 校验 → chat 非流式 + 流式（未配置 DeepSeek Key 时验证 400 降级）。

退出码 0 表示全链路通过；非 0 表示有失败步骤（会打印服务端日志尾部）。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VIDEO = PROJECT_ROOT / "tests" / "fixtures" / "sample.mp4"
DEFAULT_PORT = 8799


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class Ctx:
    """记录步骤结果与耗时。"""

    def __init__(self) -> None:
        self.results: List[Tuple[str, bool, str]] = []
        self.t0 = time.time()

    def check(self, name: str, condition: Any, detail: str = "") -> bool:
        ok = bool(condition)
        self.results.append((name, ok, detail))
        elapsed = time.time() - self.t0
        print(f"[{elapsed:7.1f}s] {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
        return ok

    @property
    def failed(self) -> List[str]:
        return [name for name, ok, _ in self.results if not ok]


def _free_port(start: int = DEFAULT_PORT, attempts: int = 20) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"找不到可用端口（尝试 {start}-{start + attempts - 1}）")


class ServerProcess:
    """拉起 `python -m vanalyze --port N` 子进程，并收集输出。"""

    def __init__(self, port: int, env_overrides: Optional[Dict[str, str]] = None) -> None:
        self.port = port
        self.proc: Optional[subprocess.Popen[str]] = None
        self.lines: List[str] = []
        self._reader: Optional[threading.Thread] = None
        env = dict(os.environ)
        if env_overrides:
            env.update({str(key): str(value) for key, value in env_overrides.items()})
        env["PYTHONUTF8"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vanalyze", "--port", str(port)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._reader = threading.Thread(target=self._drain, name="smoke-server-log", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
            if len(self.lines) > 4000:
                del self.lines[:1000]

    def log_tail(self, count: int = 40) -> str:
        return "\n".join(self.lines[-count:])

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if os.name == "nt":
                # Windows 上 venv 的 python.exe 会转启基础解释器，需要整棵进程树结束
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True,
                    timeout=20,
                )
            else:  # pragma: no cover - 本项目以 Windows 为主
                self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except Exception:  # noqa: BLE001 - 进程可能已被 taskkill 结束
                pass
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _wait_health(client: Any, timeout: float) -> Dict[str, Any]:
    """等待目标服务健康；严格校验是「视频分析后端」的 health JSON（避免端口被其他 mock 占用误判）。"""
    deadline = time.time() + timeout
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            response = client.get("/api/health", timeout=3.0)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("ok") is True and "version" in payload:
                    return payload
                last_error = ValueError(f"/api/health 不像本服务响应：{str(payload)[:120]}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.25)
    raise SystemExit(f"服务未就绪（{timeout}s）：{last_error}")


def _poll_job(client: Any, job_id: str, timeout: float) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_line = ""
    job: Dict[str, Any] = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", timeout=10.0).json()["job"]
        line = f"{job['status']} / {job['stage_label']} {round(float(job.get('progress') or 0) * 100)}%"
        if line != last_line:
            print(f"          作业状态：{line}")
            last_line = line
        if job["status"] in ("done", "error"):
            return job
        time.sleep(1.0)
    raise SystemExit(f"作业 {job_id} 超时未结束（>{timeout}s），最后状态：{last_line}")


def _parse_sse(response: Any) -> List[Tuple[str, Dict[str, Any]]]:
    events: List[Tuple[str, Dict[str, Any]]] = []
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            name = "message"
            data_lines: List[str] = []
            for line in block.splitlines():
                if line.startswith("event:"):
                    name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
            if not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except ValueError:
                payload = {"raw": "\n".join(data_lines)}
            events.append((name, payload if isinstance(payload, dict) else {"value": payload}))
    return events


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="视频分析 · DeepSeek 服务端真实链路冒烟")
    parser.add_argument("--base", default=None, help="已有服务地址（不传则自动拉起临时服务）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"临时服务端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--video", default=str(DEFAULT_VIDEO), help="测试用视频路径")
    parser.add_argument("--timeout", type=float, default=1800.0, help="等待作业完成的最长秒数（默认 1800）")
    parser.add_argument("--startup-timeout", type=float, default=120.0, help="等待服务启动的最长秒数")
    parser.add_argument("--keep-server", action="store_true", help="结束后保留临时服务（默认关停）")
    parser.add_argument("--keep-job", action="store_true", help="结束后保留作业记录（默认删除，便于真实数据目录保持干净）")
    parser.add_argument("--question", default="这个视频主要讲了什么？请给出带时间戳的要点。")
    args = parser.parse_args(argv)

    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        print(f"缺少 httpx：{exc}。请先 pip install -r requirements.txt", file=sys.stderr)
        return 2

    video = Path(args.video)
    if not video.is_file():
        print(f"测试视频不存在：{video}", file=sys.stderr)
        return 2

    server: Optional[ServerProcess] = None
    ctx = Ctx()
    base = args.base
    if not base:
        port = _free_port(args.port)
        print(f"启动临时服务：python -m vanalyze --port {port}")
        server = ServerProcess(port)
        base = f"http://127.0.0.1:{port}"
    base = str(base).rstrip("/")
    print(f"服务地址：{base}")
    print(f"测试视频：{video}")

    client = httpx.Client(base_url=base, trust_env=False)
    exit_code = 0
    job_id: Optional[str] = None
    try:
        health = _wait_health(client, args.startup_timeout)
        ctx.check("GET /api/health", health.get("ok") is True)
        print(
            "          version=%s deepseek_configured=%s asr=%s"
            % (
                health.get("version"),
                health.get("deepseek_configured"),
                json.dumps(health.get("asr"), ensure_ascii=False),
            )
        )
        key_configured = bool(health.get("deepseek_configured"))

        # ---- 上传 ----
        t_upload = time.time()
        with video.open("rb") as handle:
            response = client.post(
                "/api/jobs/upload",
                files={"file": (video.name, handle, "video/mp4")},
                data={"title": f"冒烟-{video.stem}"},
                timeout=600.0,
            )
        ctx.check("POST /api/jobs/upload 200", response.status_code == 200, response.text[:200])
        if response.status_code != 200:
            raise SystemExit(response.text[:500])
        job = response.json()["job"]
        job_id = job["id"]
        print(f"          上传耗时 {time.time() - t_upload:.2f}s，job_id={job_id}")
        ctx.check("作业含阶段列表", len(job.get("stages") or []) >= 6)
        ctx.check("作业含 media_url", bool(job.get("media_url")))

        # ---- 轮询 ----
        t_job = time.time()
        job = _poll_job(client, job_id, args.timeout)
        job_elapsed = time.time() - t_job
        ctx.check("作业终态 done", job["status"] == "done", job.get("error") or job["status"])
        if job["status"] != "done":
            raise SystemExit(f"作业失败：{job.get('error')}")
        ctx.check("进度 100%", abs(float(job.get("progress") or 0) - 1.0) < 1e-6, str(job.get("progress")))
        ctx.check("全部阶段 done", all(s.get("status") == "done" for s in job.get("stages") or []))
        print(f"          作业总耗时 {job_elapsed:.1f}s，title={job.get('title')!r}，duration={job.get('duration')}s")
        print("          阶段时间线：" + " → ".join(f"{s.get('label')}({s.get('status')})" for s in job.get("stages") or []))

        # ---- 产物 ----
        transcript_r = client.get(f"/api/jobs/{job_id}/transcript", timeout=60.0)
        ctx.check("GET transcript 200", transcript_r.status_code == 200, transcript_r.text[:150])
        if transcript_r.status_code == 200:
            transcript = transcript_r.json()["transcript"]
            segments = transcript.get("segments") or []
            text = transcript.get("text") or ""
            ctx.check("转写非空", bool(segments) and bool(text.strip()), f"{len(segments)} 段 / {len(text)} 字")
            print(
                "          转写：%d 段 / %d 字 / language=%r"
                % (len(segments), len(text), transcript.get("language"))
            )
            print("          转写开头：" + text.strip().replace("\n", " ")[:160])
            ctx.check("has_transcript 标记", job.get("has_transcript") is True)
            ctx.check("segment_count 一致", int(job.get("segment_count") or 0) == len(segments))

        summary_r = client.get(f"/api/jobs/{job_id}/summary", timeout=60.0)
        ctx.check("GET summary 200", summary_r.status_code == 200, summary_r.text[:150])
        if summary_r.status_code == 200:
            summary = summary_r.json()["summary"]
            print(
                "          总结：generated_by=%r one_liner=%r chapters=%d key_points=%d"
                % (
                    summary.get("generated_by"),
                    (summary.get("one_liner") or "")[:80],
                    len(summary.get("chapters") or []),
                    len(summary.get("key_points") or []),
                )
            )
            if not summary.get("generated_by"):
                print("          （降级总结：未配置 DeepSeek Key，符合契约）")
            ctx.check("has_summary 标记", job.get("has_summary") is True)

        ocr_r = client.get(f"/api/jobs/{job_id}/ocr", timeout=60.0)
        ctx.check("GET ocr 200", ocr_r.status_code == 200, ocr_r.text[:150])
        if ocr_r.status_code == 200:
            ocr = ocr_r.json()["ocr"]
            frames = ocr.get("frames") or []
            ctx.check("OCR 结果可解析", isinstance(frames, list))
            print("          画面文字：%d 帧 / 文本 %d 字" % (len(frames), len(ocr.get("text") or "")))

        thumbnail = client.get(f"/api/jobs/{job_id}/thumbnail", timeout=30.0)
        print("          缩略图：HTTP %d %s" % (thumbnail.status_code, thumbnail.headers.get("content-type", "")))

        # ---- 媒体 Range ----
        media_head = client.get(f"/api/jobs/{job_id}/media", headers={"Range": "bytes=0-1023"}, timeout=60.0)
        ctx.check("media Range 206", media_head.status_code == 206, str(media_head.status_code))
        if media_head.status_code == 206:
            ctx.check("Range 长度 1024", len(media_head.content) == 1024, str(len(media_head.content)))
            print("          Content-Range: %s" % media_head.headers.get("content-range"))

        # ---- 导出 ----
        for fmt in ("txt", "srt", "md"):
            export = client.get(f"/api/jobs/{job_id}/export?format={fmt}", timeout=60.0)
            ctx.check(f"导出 {fmt}", export.status_code == 200 and len(export.content) > 0, str(export.status_code))

        # ---- 问答 ----
        if key_configured:
            _chat_checks(ctx, client, job_id, args.question)
        else:
            response = client.post(
                f"/api/jobs/{job_id}/chat",
                json={"question": args.question, "stream": False},
                timeout=60.0,
            )
            expected = response.status_code == 400 and "API Key" in json.dumps(
                response.json(), ensure_ascii=False
            )
            ctx.check("无 Key 时 chat 400 中文降级", expected, f"{response.status_code} {response.text[:120]}")
            print("          （未配置 DeepSeek Key，跳过真实问答；写入 Key 后重跑即可验证）")

        # ---- 清理见 finally（默认删除作业，保持真实 data/ 目录整洁）----
    except SystemExit as exc:
        if exc.code not in (0, None):
            ctx.results.append(("流程中断", False, str(exc)))
    finally:
        if job_id and not args.keep_job:
            try:
                deleted = client.delete(f"/api/jobs/{job_id}", timeout=30.0)
                if deleted.status_code == 200:
                    print(f"          已清理作业 {job_id}")
            except Exception:  # noqa: BLE001 - 清理失败不影响结论
                print(f"          清理作业 {job_id} 失败（可手动删除）")
        client.close()
        if server is not None:
            if args.keep_server:
                print(f"按 --keep-server 保留临时服务：{base}")
                server = None
            else:
                server.stop()
        total = time.time() - ctx.t0
        failed = ctx.failed
        print("\n" + "=" * 64)
        print(f"真实联调结果：{len(ctx.results) - len(failed)}/{len(ctx.results)} 通过，总耗时 {total:.1f}s")
        if failed:
            exit_code = 1
            print("失败项：" + "；".join(failed))
            if server is not None and server.lines:
                print("-" * 32 + " 服务端日志尾部 " + "-" * 32)
                print(server.log_tail(60))
        print("=" * 64)
    return exit_code


def _chat_checks(ctx: Ctx, client: Any, job_id: str, question: str) -> None:
    """有 Key 时验证非流式 + 流式问答。"""
    t_chat = time.time()
    response = client.post(
        f"/api/jobs/{job_id}/chat",
        json={"question": question, "stream": False},
        timeout=300.0,
    )
    ctx.check("chat 非流式 200", response.status_code == 200, response.text[:200])
    if response.status_code == 200:
        data = response.json()
        answer = data.get("answer") or ""
        citations = data.get("citations") or []
        ctx.check("非流式回答非空", bool(answer.strip()), f"{len(answer)} 字")
        ctx.check("非流式 citations", isinstance(citations, list))
        print("          非流式耗时 %.1fs，回答 %d 字，引用 %d 条" % (time.time() - t_chat, len(answer), len(citations)))
        print("          回答开头：" + answer.strip()[:160].replace("\n", " "))

    t_stream = time.time()
    events: List[Tuple[str, Dict[str, Any]]] = []
    with client.stream(
        "POST",
        f"/api/jobs/{job_id}/chat",
        json={"question": question, "stream": True},
        timeout=300.0,
    ) as stream:
        ctx.check("chat 流式 200", stream.status_code == 200, str(stream.status_code))
        ctx.check(
            "SSE Content-Type",
            "text/event-stream" in stream.headers.get("content-type", ""),
            stream.headers.get("content-type", ""),
        )
        if stream.status_code == 200:
            events = _parse_sse(stream)
    names = [name for name, _ in events]
    ctx.check("SSE 有 delta", names.count("delta") > 0, str(names[:16]))
    ctx.check("SSE citations 在 done 前", "citations" in names and "done" in names and names.index("citations") < names.index("done"))
    ctx.check("SSE 以 done 结束", bool(events) and events[-1][0] == "done", str(events[-1][0] if events else None))
    answer = "".join(payload.get("text", "") for name, payload in events if name == "delta")
    citations = next((payload.get("items") for name, payload in events if name == "citations"), None)
    print(
        "          流式耗时 %.1fs，事件 %d 个（delta=%d），回答 %d 字，引用 %s 条"
        % (
            time.time() - t_stream,
            len(events),
            names.count("delta"),
            len(answer),
            len(citations) if isinstance(citations, list) else "?",
        )
    )
    ctx.check("流式回答非空", bool(answer.strip()))
    messages = client.get(f"/api/jobs/{job_id}/messages", timeout=30.0).json().get("messages") or []
    ctx.check("问答消息落库", len(messages) >= 4, f"{len(messages)} 条")


if __name__ == "__main__":
    raise SystemExit(main())
