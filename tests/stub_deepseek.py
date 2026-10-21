"""本地 OpenAI 兼容的 DeepSeek stub 服务（测试专用，不依赖真实 Key）。

用途
----
验收测试在无用户 Key 的环境下验证「总结 / 问答」链路。stub 实现契约 §6.6
所需的 ``POST {base_url}/chat/completions``：

- 普通请求：返回固定中文回答；
- 请求带 ``response_format={"type":"json_object"}`` 或提示词要求 JSON：
  返回字段完整的中文 Summary JSON（title/one_liner/overview/topics/chapters/
  key_points/keywords/quotes/visual_notes）；
- ``stream: true``：按 SSE 返回 ``data: {"choices":[{"delta":{"content":"..."}}]}``
  分片，末尾 ``data: [DONE]``；
- 兼容 ``Authorization: Bearer <任意 key>``。

为测试 JSON 修复 / 章节兜底等能力，提示词里可加入以下标记（大小写敏感）：

============================  ==========================================
``[STUB:FENCED]``             返回 ```json 围栏包裹的 JSON
``[STUB:TRUNCATED]``          JSON 前后夹普通中文说明文字
``[STUB:SIMPLE_JSON]``        返回 {"ok": true, "message": "中文测试值"}
``[STUB:NOT_JSON]``           始终返回普通文本（用于验证重试后抛错）
============================  ==========================================

另外可通过实例属性控制返回内容：

- ``summary_mode = "valid"``（默认）/ ``"invalid_chapters"``（非法章节用
  于验证 summarize 的时间戳兜底修正）；
- ``answer`` / ``stream_answer_text``：普通回答与流式回答的文本。

用法
----
进程内启动（pytest fixture / e2e 脚本）::

    from stub_deepseek import StubDeepSeek
    stub = StubDeepSeek().start()
    settings.deepseek_base_url = stub.base_url
    ...
    stub.stop()

独立启动（手动调试）::

    .venv\\Scripts\\python.exe tests\\stub_deepseek.py --port 8777
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777

#: 普通请求（非 JSON 模式）返回的固定中文回答
FIXED_ANSWER = (
    "这是本地 stub DeepSeek 的固定回答：视频中提到了语音转文字测试和今天的天气，"
    "画面中还出现了「测试画面」字样。"
)

#: SSE 流式回答的默认文本
FIXED_STREAM_ANSWER = "这是本地 stub 的流式回答：视频主要讲了语音转文字与画面文字测试。"

#: SSE 分片大小（字符数）
SSE_CHUNK_SIZE = 8

#: 提示词标记
MARKER_FENCED = "[STUB:FENCED]"
MARKER_TRUNCATED = "[STUB:TRUNCATED]"
MARKER_SIMPLE_JSON = "[STUB:SIMPLE_JSON]"
MARKER_NOT_JSON = "[STUB:NOT_JSON]"
#: 流式请求时强制返回 HTTP 错误（验证问答 SSE 的 error 事件；非流式不受影响）
MARKER_STREAM_401 = "[STUB:STREAM_401]"
MARKER_STREAM_500 = "[STUB:STREAM_500]"

#: Summary JSON 中出现的固定中文串（测试可据此断言）
STUB_SUMMARY_TITLE = "视频分析系统自检：语音转文字与画面文字"
STUB_SUMMARY_ONE_LINER = "一段用于验证语音转文字、画面文字与总结链路的自检视频。"
STUB_VISUAL_NOTES = ["第一章  测试画面", "画面文字 OCR 测试", "语音转文字测试"]


def summary_payload() -> Dict[str, Any]:
    """字段完整的 Summary JSON（章节落在 0..12s，适配 sample.mp4）。"""
    chapters = [
        (0.0, 2.0, "开场与测试目标", "介绍这是一段视频分析系统的自检视频。"),
        (2.0, 4.0, "天气与语音", "提到今天天气很好，准备测试语音转文字功能。"),
        (4.0, 6.0, "语音转文字测试", "说明正在测试语音转文字功能，检查识别准确率。"),
        (6.0, 8.0, "画面文字 OCR", "画面出现「第一章 测试画面」等中文字样。"),
        (8.0, 10.0, "识别结果核对", "请确认转写结果是否准确，并核对画面文字。"),
        (10.0, 11.9, "结尾与自检结论", "总结本次自检覆盖语音转文字、OCR 与总结生成。"),
    ]
    return {
        "title": STUB_SUMMARY_TITLE,
        "one_liner": STUB_SUMMARY_ONE_LINER,
        "overview": (
            "## 视频概览\n\n"
            "这是一段约 12 秒的中文自检视频，用于验证「视频分析 · DeepSeek」的完整链路。\n\n"
            "- **语音转文字**：音频朗读了天气预报与自检说明；\n"
            "- **画面文字**：三张幻灯片分别出现了「第一章 测试画面」「画面文字 OCR 测试」"
            "「语音转文字测试」；\n"
            "- **总结生成**：本段文字即为 stub DeepSeek 返回的固定总结内容。\n\n"
            "> 说明：以上内容由本地 stub 生成，仅用于自动化验收。"
        ),
        "topics": ["语音转文字", "画面文字识别", "视频分析自检"],
        "chapters": [
            {"start": s, "end": e, "title": title, "summary": desc}
            for s, e, title, desc in chapters
        ],
        "key_points": [
            "视频是一段约 12 秒的中文自检素材。",
            "音频中包含「今天天气很好」等普通话语音。",
            "画面文字包含「第一章 测试画面」。",
            "自检覆盖语音转文字、OCR 与结构化总结。",
            "结果用于验证 DeepSeek 调用链路。",
        ],
        "keywords": ["语音转文字", "OCR", "DeepSeek", "自检", "天气"],
        "quotes": [
            "【00:01】今天天气很好，我们来测试语音转文字功能。",
            "【00:05】这是视频分析系统的自检音频。",
        ],
        "visual_notes": list(STUB_VISUAL_NOTES),
    }


def invalid_chapters_payload() -> Dict[str, Any]:
    """其余字段合法、章节时间戳非法的 Summary JSON（验证兜底修正）。"""
    payload = summary_payload()
    payload.update(
        {
            "title": "章节时间戳兜底测试",
            "one_liner": "用于验证非法章节时间戳会被修正。",
            "overview": "## 兜底测试\n\n本段用于验证 summarize 的章节时间戳兜底逻辑。",
            "chapters": [
                {"start": 8.0, "end": 3.0, "title": "起点大于终点", "summary": "非法章节一。"},
                {"start": -5.0, "end": 4.0, "title": "负起点", "summary": "非法章节二。"},
                {"start": 10.0, "end": 999999.0, "title": "超出总时长", "summary": "非法章节三。"},
                {"start": 0.0, "end": 2.0, "title": "唯一合法章节", "summary": "合法章节。"},
            ],
        }
    )
    return payload


class StubDeepSeek:
    """进程内可启动 / 停止的 OpenAI 兼容 stub 服务。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 0) -> None:
        self._httpd = ThreadingHTTPServer((host, int(port)), _StubHandler)
        self._httpd.daemon_threads = True  # type: ignore[attr-defined]
        self._httpd.stub = self  # type: ignore[attr-defined]
        self.host = host
        self.port = int(self._httpd.server_address[1])
        self._lock = threading.Lock()
        self._requests: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        #: "valid" | "invalid_chapters"
        self.summary_mode = "valid"
        #: 普通（非 JSON）回答
        self.answer = FIXED_ANSWER
        #: 流式回答文本
        self.stream_answer_text = FIXED_STREAM_ANSWER

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> "StubDeepSeek":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="stub-deepseek",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001 - 关闭时的异常不影响测试结果
            pass
        try:
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        self._thread = None

    # ------------------------------------------------------------------
    # 属性 / 记录
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def urls(self) -> List[str]:
        """已被请求的路径列表。"""
        with self._lock:
            return [item["path"] for item in self._requests]

    @property
    def requests(self) -> List[Dict[str, Any]]:
        """已收到的请求（含 headers 与解析后的 JSON body）。"""
        with self._lock:
            return [dict(item) for item in self._requests]

    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._requests[-1]) if self._requests else None

    def reset(self) -> None:
        """清空记录并恢复默认行为（每个测试开始前调用）。"""
        with self._lock:
            self._requests.clear()
        self.summary_mode = "valid"
        self.answer = FIXED_ANSWER
        self.stream_answer_text = FIXED_STREAM_ANSWER

    def clear_requests(self) -> None:
        with self._lock:
            self._requests.clear()

    def _record(self, path: str, headers: Dict[str, str], body: Dict[str, Any]) -> None:
        with self._lock:
            self._requests.append(
                {
                    "path": path,
                    "headers": {str(k).lower(): str(v) for k, v in headers.items()},
                    "body": body,
                    "at": time.time(),
                }
            )

    # ------------------------------------------------------------------
    # 响应内容生成
    # ------------------------------------------------------------------
    def current_summary_payload(self) -> Dict[str, Any]:
        if self.summary_mode == "invalid_chapters":
            return invalid_chapters_payload()
        return summary_payload()

    def response_text(self, prompt: str, json_mode: bool = False) -> str:
        """根据提示词标记 / JSON 模式生成非流式回答文本。"""
        if MARKER_NOT_JSON in prompt:
            return "抱歉，这里是普通中文文本回答，没有 JSON 数据。"

        want_json = (
            json_mode
            or MARKER_FENCED in prompt
            or MARKER_TRUNCATED in prompt
            or MARKER_SIMPLE_JSON in prompt
        )
        if not want_json:
            return self.answer

        if MARKER_SIMPLE_JSON in prompt:
            payload: Dict[str, Any] = {"ok": True, "message": "中文测试值"}
        else:
            payload = self.current_summary_payload()
        text = json.dumps(payload, ensure_ascii=False)
        if MARKER_FENCED in prompt:
            return "```json\n" + text + "\n```"
        if MARKER_TRUNCATED in prompt:
            return (
                "好的，我先梳理一下视频内容，结构化结果如下：\n\n"
                + text
                + "\n\n以上是本次视频的结构化总结，供参考。"
            )
        return text

    def stream_text(self, prompt: str) -> str:
        """流式回答文本（JSON 模式也可返回 JSON 文本分片）。"""
        if MARKER_SIMPLE_JSON in prompt:
            return json.dumps({"ok": True, "message": "中文测试值"}, ensure_ascii=False)
        return self.stream_answer_text or FIXED_STREAM_ANSWER


class _StubHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理：只实现 /chat/completions 与少量探活接口。"""

    protocol_version = "HTTP/1.1"
    server_version = "StubDeepSeek/1.0"

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @property
    def stub(self) -> StubDeepSeek:
        return self.server.stub  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆盖基类
        """静默日志：stub 的访问日志会干扰 pytest 输出。"""

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass
        self.close_connection = True

    @staticmethod
    def _messages_text(body: Dict[str, Any]) -> str:
        messages = body.get("messages") or []
        parts: List[str] = []
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):  # 兼容多模态 content 数组
                        for item in content:
                            if isinstance(item, dict) and isinstance(item.get("text"), str):
                                parts.append(item["text"])
        return "\n".join(parts)

    @staticmethod
    def _is_json_mode(body: Dict[str, Any]) -> bool:
        response_format = body.get("response_format")
        if isinstance(response_format, dict):
            return str(response_format.get("type") or "").lower() == "json_object"
        return False

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - 覆盖基类
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health", "/v1/health"):
            self._send_json({"ok": True, "stub": True, "base_url": self.stub.base_url})
            return
        if path.endswith("/models"):
            self._send_json({"object": "list", "data": [{"id": "deepseek-chat"}]})
            return
        self._send_json({"detail": "接口不存在"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - 覆盖基类
        path = self.path.split("?")[0].rstrip("/")
        if not path.endswith("/chat/completions"):
            self._send_json({"detail": "接口不存在"}, 404)
            return
        body = self._read_json_body()
        self.stub._record(self.path, dict(self.headers), body)
        prompt = self._messages_text(body)
        if bool(body.get("stream")):
            if MARKER_STREAM_401 in prompt:
                self._send_json({"error": {"message": "stub 强制 401"}}, 401)
                return
            if MARKER_STREAM_500 in prompt:
                self._send_json({"error": {"message": "stub 强制 500"}}, 500)
                return
            self._send_stream(prompt)
            return
        json_mode = self._is_json_mode(body)
        content = self.stub.response_text(prompt, json_mode=json_mode)
        self._send_json(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(body.get("model") or "deepseek-chat"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 16, "completion_tokens": 32, "total_tokens": 48},
            }
        )

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    def _write_sse_data(self, payload: Any) -> None:
        if isinstance(payload, str):
            line = f"data: {payload}\n\n"
        else:
            line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _send_stream(self, prompt: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        answer = self.stub.stream_text(prompt)
        try:
            self._write_sse_data(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            for start in range(0, len(answer), SSE_CHUNK_SIZE):
                self._write_sse_data(
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": answer[start : start + SSE_CHUNK_SIZE]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                time.sleep(0.002)
            self._write_sse_data(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            self._write_sse_data("[DONE]")
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端提前断开
            pass
        finally:
            self.close_connection = True


def start_stub(host: str = DEFAULT_HOST, port: int = 0) -> StubDeepSeek:
    """便捷函数：创建并启动 stub，返回实例（可 ``stub.stop()``）。"""
    return StubDeepSeek(host=host, port=port).start()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="本地 OpenAI 兼容 DeepSeek stub（测试用）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    args = parser.parse_args(argv)
    try:
        stub = start_stub(args.host, args.port)
    except OSError as exc:
        print(f"stub 启动失败（端口 {args.port} 可能被占用）：{exc}")
        return 1
    print(f"stub DeepSeek 已启动：{stub.base_url}/chat/completions")
    print("按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstub 已停止。")
    finally:
        stub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
