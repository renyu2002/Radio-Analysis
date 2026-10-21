"""DeepSeek（OpenAI 兼容）客户端。

- 请求：``{base_url}/chat/completions``，``Authorization: Bearer``，httpx；
- 重试：429 / 5xx / 网络超时 重试 2 次（1s、3s 指数退避）；
- 错误统一为中文 :class:`DeepSeekError`，可直接展示给用户；
- ``deepseek-reasoner`` 不支持 temperature / response_format，内部自动省略；
- ``chat_json`` 负责剥 ```json 围栏、截取首尾大括号，失败重试一次。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
NO_KEY_MESSAGE = "尚未配置 DeepSeek API Key，请在右上角「设置」中填写。"

_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
_MAX_RETRIES = 2  # 失败后重试 2 次（总计 3 次尝试）
_BACKOFF_SECONDS = (1.0, 3.0)
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class DeepSeekError(Exception):
    """DeepSeek 调用失败；``str(exc)`` 为可直接展示的中文消息。"""


class _RetryableError(Exception):
    """内部使用：可重试的错误（429/5xx/网络错误）。"""


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------
def _api_key(settings) -> str:
    key = str(getattr(settings, "deepseek_api_key", "") or "").strip()
    if key:
        return key
    # 兜底：调用方可能传入了未合入环境变量的 Settings
    return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def is_configured(settings) -> bool:
    """是否已配置 DeepSeek API Key（设置或环境变量）。"""
    return bool(_api_key(settings))


def _base_url(settings) -> str:
    base = str(getattr(settings, "deepseek_base_url", "") or DEFAULT_BASE_URL).strip()
    return base.rstrip("/") or DEFAULT_BASE_URL


def _endpoint(settings) -> str:
    base = _base_url(settings)
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _deployment_url(settings) -> str:
    """用于错误消息展示的 Base URL（不含敏感信息）。"""
    return _base_url(settings)


def _resolve_model(settings, model: Optional[str]) -> str:
    if model and str(model).strip():
        return str(model).strip()
    summary_model = getattr(settings, "resolved_summary_model", None)
    if summary_model:
        return str(summary_model).strip()
    return str(getattr(settings, "deepseek_model", "deepseek-chat") or "deepseek-chat").strip()


def _is_reasoner(model: str) -> bool:
    return "reasoner" in (model or "").lower()


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# 请求构造与错误映射
# ---------------------------------------------------------------------------
def _build_payload(
    messages: List[Dict[str, Any]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    stream: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": list(messages or []),
        "max_tokens": int(max_tokens or 4000),
        "stream": bool(stream),
    }
    if not _is_reasoner(model):
        payload["temperature"] = float(temperature)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
    return payload


def _error_detail(body: str) -> str:
    """从错误响应里提取可读信息。"""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text[:200]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)[:300]
        message = data.get("message") or data.get("detail")
        if message:
            return str(message)[:300]
    return text[:200]


def _status_message(status: int, body: str, settings) -> str:
    detail = _error_detail(body)
    base = _deployment_url(settings)
    if status == 401:
        return "DeepSeek API Key 无效（401），请在设置中检查。"
    if status == 402:
        return "DeepSeek 账户余额不足（402），请充值后再试。"
    if status == 403:
        return "DeepSeek 拒绝访问（403），请检查 API Key 的权限。"
    if status == 404:
        return f"DeepSeek 接口不存在（404），请检查 Base URL 设置（当前：{base}）。"
    if status == 429:
        return "DeepSeek 请求过于频繁（429），请稍后重试。"
    if status >= 500:
        return f"DeepSeek 服务暂时不可用（{status}），请稍后重试。"
    suffix = f"：{detail}" if detail else ""
    return f"DeepSeek 请求失败（HTTP {status}）{suffix}"


def _network_message(exc: BaseException, settings) -> str:
    base = _deployment_url(settings)
    if isinstance(exc, httpx.TimeoutException):
        return f"DeepSeek 请求超时（{base}），请检查网络后重试。"
    if isinstance(exc, httpx.ConnectError):
        return f"无法连接 DeepSeek 服务（{base}），请检查网络或 Base URL 设置。"
    return f"DeepSeek 网络请求失败：{exc}"


def _sleep_backoff(attempt: int) -> None:
    delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
    time.sleep(delay)


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
def chat(
    settings,
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    json_mode: bool = False,
) -> str:
    """同步对话，返回助手回复文本。"""
    api_key = _api_key(settings)
    if not api_key:
        raise DeepSeekError(NO_KEY_MESSAGE)
    resolved_model = _resolve_model(settings, model)
    payload = _build_payload(
        messages,
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        stream=False,
    )
    endpoint = _endpoint(settings)
    last_error: Optional[str] = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(endpoint, headers=_headers(api_key), json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = _network_message(exc, settings)
            if attempt < _MAX_RETRIES:
                logger.warning("DeepSeek 请求异常（%s/%s）：%s，准备重试", attempt + 1, _MAX_RETRIES, exc)
                _sleep_backoff(attempt)
                continue
            raise DeepSeekError(last_error) from exc

        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                raise DeepSeekError("DeepSeek 返回了无法解析的响应（非 JSON）。") from exc
            return _extract_message_content(data)

        message = _status_message(response.status_code, response.text, settings)
        if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            last_error = message
            logger.warning(
                "DeepSeek 请求失败（HTTP %s，第 %s/%s 次），准备重试",
                response.status_code,
                attempt + 1,
                _MAX_RETRIES,
            )
            _sleep_backoff(attempt)
            continue
        raise DeepSeekError(message)

    raise DeepSeekError(last_error or "DeepSeek 请求失败：未知错误。")


def _extract_message_content(data: Dict[str, Any]) -> str:
    choices = (data or {}).get("choices") or []
    if not choices:
        raise DeepSeekError("DeepSeek 返回了空响应（没有 choices）。")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        # 部分推理模型只返回 reasoning_content 时也尽量给出文本
        content = message.get("reasoning_content") or ""
    if isinstance(content, list):
        # 兼容多段内容
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
        content = "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# chat_stream
# ---------------------------------------------------------------------------
def chat_stream(
    settings,
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 4000,
) -> Iterator[str]:
    """流式对话：返回逐段文本的迭代器（先同步完成参数校验）。"""
    api_key = _api_key(settings)
    if not api_key:
        raise DeepSeekError(NO_KEY_MESSAGE)
    resolved_model = _resolve_model(settings, model)
    payload = _build_payload(
        messages,
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=False,
        stream=True,
    )
    endpoint = _endpoint(settings)
    return _iter_stream(settings, endpoint, payload, api_key, resolved_model)


def _iter_stream(
    settings,
    endpoint: str,
    payload: Dict[str, Any],
    api_key: str,
    model: str,
) -> Iterator[str]:
    last_error: Optional[str] = None

    for attempt in range(_MAX_RETRIES + 1):
        emitted = False
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                with client.stream(
                    "POST", endpoint, headers=_headers(api_key), json=payload
                ) as response:
                    if response.status_code != 200:
                        body = ""
                        try:
                            body = response.read().decode("utf-8", errors="replace")
                        except Exception:  # pragma: no cover
                            body = ""
                        message = _status_message(response.status_code, body, settings)
                        if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                            raise _RetryableError(message)
                        raise DeepSeekError(message)

                    for raw_line in response.iter_lines():
                        line = (raw_line or "").strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            return
                        try:
                            event = json.loads(data)
                        except (ValueError, TypeError):
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            emitted = True
                            yield str(content)
            return
        except DeepSeekError:
            raise
        except _RetryableError as exc:
            last_error = str(exc)
            if emitted:
                raise DeepSeekError("DeepSeek 流式响应中断，请重试。") from exc
            if attempt < _MAX_RETRIES:
                logger.warning("DeepSeek 流式请求失败（%s/%s）：%s，准备重试", attempt + 1, _MAX_RETRIES, exc)
                _sleep_backoff(attempt)
                continue
            raise DeepSeekError(last_error) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = _network_message(exc, settings)
            if emitted:
                raise DeepSeekError("DeepSeek 流式响应中断，请重试。") from exc
            if attempt < _MAX_RETRIES:
                logger.warning("DeepSeek 流式请求异常（%s/%s）：%s，准备重试", attempt + 1, _MAX_RETRIES, exc)
                _sleep_backoff(attempt)
                continue
            raise DeepSeekError(last_error) from exc

    raise DeepSeekError(last_error or "DeepSeek 流式请求失败：未知错误。")


# ---------------------------------------------------------------------------
# chat_json
# ---------------------------------------------------------------------------
def _parse_json_text(text: str) -> Dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty response")
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, flags=re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object found")
    data = json.loads(candidate[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("json root is not an object")
    return data


def chat_json(
    settings,
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_tokens: int = 4000,
) -> Dict[str, Any]:
    """要求模型输出 JSON 对象；解析失败自动重试一次。"""
    resolved_model = _resolve_model(settings, model)
    base_messages = list(messages or [])
    last_text = ""

    for attempt in range(2):
        if attempt == 0:
            attempt_messages = base_messages
        else:
            attempt_messages = base_messages + [
                {"role": "assistant", "content": last_text[:4000]},
                {
                    "role": "user",
                    "content": (
                        "你上面的输出不是合法的 JSON 对象。请重新输出：只输出一个 JSON 对象，"
                        "不要 markdown 代码块围栏，不要任何解释文字。"
                    ),
                },
            ]
        last_text = chat(
            settings,
            attempt_messages,
            model=resolved_model,
            temperature=0.3,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            return _parse_json_text(last_text)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("JSON 解析失败（第 %s 次）：%s", attempt + 1, exc)

    raise DeepSeekError("DeepSeek 未能返回合法的 JSON（已重试 1 次），请稍后再试或更换模型。")


# ---------------------------------------------------------------------------
# 连接测试
# ---------------------------------------------------------------------------
def test_connection(settings) -> Tuple[bool, str]:
    """测试 API Key / Base URL / 模型是否可用，返回 (ok, 中文消息)。"""
    if not is_configured(settings):
        return False, NO_KEY_MESSAGE
    model = _resolve_model(settings, None)
    try:
        reply = chat(
            settings,
            [{"role": "user", "content": "你好，请只回复两个字母：OK"}],
            model=model,
            temperature=0.0,
            max_tokens=16,
        )
    except DeepSeekError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - 兜底
        return False, f"DeepSeek 连接测试失败：{exc}"
    if reply and reply.strip():
        return True, f"连接成功（{model}）"
    return False, "DeepSeek 连接测试失败：返回内容为空。"
