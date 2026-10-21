"""基于视频内容的检索问答：本地 BM25 检索 + DeepSeek 流式回答。

- 读取 ``job_dir`` 内的 ``transcript.json / summary.json / ocr.json``；
- 中文 bigram + 英文词的 BM25 在 segments 上取 top-14，合并相邻段（±1）；
- 上下文拼上章节摘要与 OCR 时间线，总长度 ≤ 45000 字；
- 返回 ``(文本迭代器, citations)``；无 Key 抛 :class:`DeepSeekError`。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..models import Citation, OcrResult, Summary, Transcript
from . import deepseek

logger = logging.getLogger(__name__)

_TOP_SEGMENTS = 14
_HISTORY_LIMIT = 8
_HISTORY_ITEM_CHARS = 2000
_CONTEXT_MAX_CHARS = 45000
_TRANSCRIPT_CONTEXT_CHARS = 30000
_CHAPTER_CONTEXT_CHARS = 6000
_SUMMARY_CONTEXT_CHARS = 5000
_OCR_CONTEXT_CHARS = 4000
_CITATION_TEXT_CHARS = 300
_CITATION_LIMIT = 20

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")

_SYSTEM_PROMPT = """你是「视频分析 · DeepSeek」的视频内容分析助手。你只能依据下面提供的视频转写、画面文字（OCR）与总结信息回答问题。

规则：
1. 回答使用简体中文；
2. 引用视频中的证据时，用【MM:SS】格式标注时间戳（例如【01:23】）；
3. 如果资料中没有相关信息，直接说明「视频中没有提到」，不要编造或使用资料以外的知识；
4. 不要输出与视频内容无关的推测；如果用户的问题与视频内容无关，请礼貌说明。
"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _fmt_ts(seconds: float) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    minutes, sec = divmod(total, 60)
    return f"{minutes:02d}:{sec:02d}"


def _tokenize(text: str) -> List[str]:
    """中文连续片段取 bigram，英文/数字取小写词。"""
    tokens: List[str] = []
    if not text:
        return tokens
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    tokens.extend(word.lower() for word in _WORD.findall(text))
    return tokens


def _bm25_scores(documents: List[List[str]], query: List[str]) -> List[float]:
    """经典 BM25（k1=1.5, b=0.75）。"""
    count = len(documents)
    if count == 0:
        return []
    if not query:
        return [0.0] * count
    average_length = sum(len(doc) for doc in documents) / float(count) or 1.0
    frequencies = [Counter(doc) for doc in documents]
    doc_freq: Counter = Counter()
    for freq in frequencies:
        for term in freq:
            doc_freq[term] += 1
    k1, b = 1.5, 0.75
    scores = [0.0] * count
    for term in dict.fromkeys(query):
        df = doc_freq.get(term)
        if not df:
            continue
        idf = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
        for index, freq in enumerate(frequencies):
            tf = freq.get(term, 0)
            if not tf:
                continue
            denominator = tf + k1 * (1.0 - b + b * len(documents[index]) / average_length)
            scores[index] += idf * tf * (k1 + 1.0) / denominator
    return scores


# ---------------------------------------------------------------------------
# 读取产物
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # pragma: no cover - 产物损坏时降级
        logger.warning("读取产物失败 %s：%s", path, exc)
        return None


def _load_artifacts(job_dir: str) -> Tuple[Transcript, Summary, OcrResult]:
    base = Path(str(job_dir))
    transcript = Transcript()
    summary = Summary()
    ocr = OcrResult()
    data = _load_json(base / "transcript.json")
    if data:
        try:
            transcript = Transcript(**data)
        except Exception:  # pragma: no cover
            logger.warning("transcript.json 解析失败，使用空转写")
    data = _load_json(base / "summary.json")
    if data:
        try:
            summary = Summary(**data)
        except Exception:  # pragma: no cover
            logger.warning("summary.json 解析失败，使用空总结")
    data = _load_json(base / "ocr.json")
    if data:
        try:
            ocr = OcrResult(**data)
        except Exception:  # pragma: no cover
            logger.warning("ocr.json 解析失败，使用空 OCR")
    return transcript, summary, ocr


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _retrieve(transcript: Transcript, question: str) -> Tuple[List[Any], List[Citation]]:
    """返回 (命中的 segment 列表, citations)。"""
    segments = list(transcript.segments or [])
    if not segments:
        return [], []
    documents = [_tokenize(segment.text or "") for segment in segments]
    query_tokens = _tokenize(question or "")
    scores = _bm25_scores(documents, query_tokens)

    ranked = sorted(range(len(segments)), key=lambda index: scores[index], reverse=True)
    selected = [index for index in ranked if scores[index] > 0][:_TOP_SEGMENTS]
    if not selected:
        selected = ranked[:_TOP_SEGMENTS]

    expanded = set()
    for index in selected:
        expanded.update((index - 1, index, index + 1))
    ordered = sorted(index for index in expanded if 0 <= index < len(segments))
    hits = [segments[index] for index in ordered]

    citations: List[Citation] = []
    for segment in ordered[: _CITATION_LIMIT]:
        item = segments[segment]
        text = (item.text or "").strip()
        citations.append(
            Citation(
                segment_id=int(item.id),
                start=float(item.start),
                end=float(item.end),
                text=text[:_CITATION_TEXT_CHARS],
            )
        )
    return hits, citations


# ---------------------------------------------------------------------------
# 上下文拼装
# ---------------------------------------------------------------------------
def _build_context(
    transcript: Transcript,
    summary: Summary,
    ocr: OcrResult,
    hits: List[Any],
) -> str:
    sections: List[str] = []

    lines = [f"[{_fmt_ts(segment.start)}] {segment.text}" for segment in hits if (segment.text or "").strip()]
    if lines:
        sections.append("【相关转写片段】\n" + _truncate("\n".join(lines), _TRANSCRIPT_CONTEXT_CHARS))
    elif transcript.text:
        sections.append(
            "【视频转写节选】\n" + _truncate(transcript.text, _TRANSCRIPT_CONTEXT_CHARS)
        )

    if summary.chapters:
        chapter_lines = [
            f"[{_fmt_ts(chapter.start)}-{_fmt_ts(chapter.end)}] {chapter.title}：{chapter.summary}"
            for chapter in summary.chapters
        ]
        sections.append("【章节摘要】\n" + _truncate("\n".join(chapter_lines), _CHAPTER_CONTEXT_CHARS))

    summary_parts: List[str] = []
    if summary.one_liner:
        summary_parts.append(f"一句话结论：{summary.one_liner}")
    if summary.overview:
        summary_parts.append(f"内容概览：{summary.overview}")
    if summary.key_points:
        summary_parts.append("关键要点：\n" + "\n".join(f"- {item}" for item in summary.key_points))
    if summary.keywords:
        summary_parts.append("关键词：" + "、".join(summary.keywords))
    if summary_parts:
        sections.append("【视频总结】\n" + _truncate("\n".join(summary_parts), _SUMMARY_CONTEXT_CHARS))

    ocr_text = (ocr.text or "").strip()
    if ocr_text:
        sections.append("【画面文字（OCR）】\n" + _truncate(ocr_text, _OCR_CONTEXT_CHARS))

    header = ""
    if summary.title:
        header = f"视频标题：{summary.title}\n"
    context = header + "\n\n".join(sections)
    if len(context) > _CONTEXT_MAX_CHARS:
        context = context[:_CONTEXT_MAX_CHARS]
    return context


def _build_messages(
    context: str,
    question: str,
    history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    system = (
        _SYSTEM_PROMPT
        + "\n===== 视频资料开始 =====\n"
        + (context or "（暂无可用资料）")
        + "\n===== 视频资料结束 ====="
    )
    messages: List[Dict[str, str]] = [{"role": "system", "content": system}]

    items = list(history or [])[-_HISTORY_LIMIT:]
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        messages.append({"role": role, "content": text[:_HISTORY_ITEM_CHARS]})
    messages.append({"role": "user", "content": question})
    return messages


def _prepare(
    *,
    job_dir: str,
    question: str,
    history: Optional[List[Dict[str, Any]]],
    settings,
) -> Tuple[List[Dict[str, str]], List[Citation]]:
    question = (question or "").strip()
    if not question:
        raise deepseek.DeepSeekError("请输入要提问的问题。")
    if not deepseek.is_configured(settings):
        raise deepseek.DeepSeekError(deepseek.NO_KEY_MESSAGE)
    transcript, summary, ocr = _load_artifacts(job_dir)
    hits, citations = _retrieve(transcript, question)
    context = _build_context(transcript, summary, ocr, hits)
    messages = _build_messages(context, question, history)
    return messages, citations


def _chat_model(settings) -> str:
    return str(getattr(settings, "deepseek_model", "deepseek-chat") or "deepseek-chat").strip()


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def answer_stream(
    *,
    job_dir: str,
    question: str,
    history: List[Dict[str, Any]],
    settings,
) -> Tuple[Iterator[str], List[Citation]]:
    """流式问答：同步完成检索后返回 (文本迭代器, citations)。"""
    messages, citations = _prepare(
        job_dir=job_dir, question=question, history=history, settings=settings
    )
    stream = deepseek.chat_stream(
        settings,
        messages,
        model=_chat_model(settings),
        temperature=0.3,
        max_tokens=2000,
    )
    return stream, citations


def answer_once(
    *,
    job_dir: str,
    question: str,
    history: List[Dict[str, Any]],
    settings,
) -> Tuple[str, List[Citation]]:
    """非流式问答，返回 (回答文本, citations)。"""
    messages, citations = _prepare(
        job_dir=job_dir, question=question, history=history, settings=settings
    )
    answer = deepseek.chat(
        settings,
        messages,
        model=_chat_model(settings),
        temperature=0.3,
        max_tokens=2000,
    )
    return answer, citations
