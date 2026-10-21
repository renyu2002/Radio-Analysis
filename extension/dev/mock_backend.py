"""视频分析 · DeepSeek —— 扩展自测用本地 mock 后端。

只用于验证 Chrome 扩展的请求行为，**不是真实服务**：
  - GET  /api/health   返回契约 §8.1 结构的健康信息；
  - POST /api/jobs     严格校验并记录 tab/url 来源请求体，返回 {"job": {...}}；
  - 其他 GET            按静态文件返回 extension/ 下的文件（供 dev 测试页加载）。

端口默认 8767（绝不占用真实后端 8765）。由 dev/verify_extension.py 以内嵌线程方式启动，
也可单独运行：.\\\\.venv\\\\Scripts\\\\python.exe extension\\\\dev\\\\mock_backend.py

自测辅助脚本，不参与扩展运行时加载。
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.dirname(DEV_DIR)
DEFAULT_PORT = 8767
MOCK_VERSION = "mock-1.0.0"

EXTRA_MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class MockState:
    """记录 mock 后端收到的 POST /api/jobs 请求，供测试断言使用。"""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.jobs: list[dict] = []


def job_from_request(body: dict) -> dict:
    """按契约 §8.3 响应结构伪造一个 Job。"""
    return {
        "id": "mock-job-1",
        "status": "pending",
        "stage": "queued",
        "stage_label": "排队中",
        "progress": 0.0,
        "source_type": body.get("source_type"),
        "title": body.get("title") or "未命名视频",
        "source_url": body.get("page_url") or body.get("url"),
        "duration": body.get("duration") or 0,
        "error": None,
        "created_at": "2026-10-21T16:00:00+08:00",
    }


def validate_job_body(body) -> list[str]:
    """严格校验 POST /api/jobs 请求体（tab 来源见契约 §8.3）。"""
    if not isinstance(body, dict):
        return ["请求体必须是 JSON 对象"]
    errors: list[str] = []
    source_type = body.get("source_type")
    if source_type not in ("tab", "url"):
        return ["source_type 必须为 tab 或 url"]

    if source_type == "tab":
        required = ["page_url", "video_url", "title", "duration", "poster"]
        for key in required:
            if key not in body:
                errors.append(f"缺少字段 {key}")
        page_url = body.get("page_url")
        if not isinstance(page_url, str) or not page_url.strip():
            errors.append("page_url 必须为非空字符串")
        if "video_url" in body and body["video_url"] is not None and not isinstance(body["video_url"], str):
            errors.append("video_url 必须为字符串或 null")
        if "title" in body and not isinstance(body["title"], str):
            errors.append("title 必须为字符串")
        if "duration" in body:
            duration = body["duration"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
                errors.append("duration 必须为非负数字")
        if "poster" in body and body["poster"] is not None and not isinstance(body["poster"], str):
            errors.append("poster 必须为字符串或 null")
    else:
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            errors.append("url 必须为非空字符串")
    return errors


def _make_handler(state: MockState, root: str):
    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **EXTRA_MIME}

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

        def log_message(self, fmt, *args):  # noqa: A003 - 保持测试输出干净
            if os.environ.get("MOCK_VERBOSE"):
                super().log_message(fmt, *args)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            super().end_headers()

        def _send_json(self, payload, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/health":
                self._send_json({
                    "ok": True,
                    "version": MOCK_VERSION,
                    "deepseek_configured": False,
                    "asr": {
                        "device": "cpu",
                        "model": "large-v3",
                        "compute_type": "int8",
                        "cuda_available": False,
                    },
                    "port": state.port,
                })
                return
            super().do_GET()

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/jobs":
                self._send_json({"detail": "Not Found"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json({"detail": f"JSON 解析失败：{exc}"}, 400)
                return

            record = {
                "json": body,
                "raw": raw.decode("utf-8", "replace"),
                "headers": dict(self.headers.items()),
            }
            errors = validate_job_body(body)
            if errors:
                self._send_json({"detail": "；".join(errors)}, 400)
                return
            state.jobs.append(record)
            self._send_json({"job": job_from_request(body)})

    return Handler


def create_server(port: int = DEFAULT_PORT, root: str = EXT_DIR):
    """创建 mock HTTP 服务（未启动），返回 (server, state)。"""
    state = MockState(port)
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state, root))
    server.daemon_threads = True
    return server, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扩展自测用 mock 后端（默认端口 8767）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=EXT_DIR)
    args = parser.parse_args(argv)
    server, _state = create_server(args.port, args.root)
    print(f"mock backend: http://127.0.0.1:{args.port}  (root={args.root})")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
