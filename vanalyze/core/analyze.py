"""DeepSeek 结构化视频总结。

- 证据 = 元信息 + 带 ``[MM:SS]`` 时间戳的转写 + 画面文字（OCR）+ 可选视觉描述；
- 长转写先分块做分段摘要（map），再合并生成结构化总结（reduce）；
- 输出严格 JSON，字段即 :class:`~vanalyze.models.Summary`；
- 章节时间戳做兜底校验（非法/越界按总时长均分修正）；
- 未配置 Key 或 DeepSeek 调用失败时降级为转写节选，**不抛错、不阻断作业**。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models import Chapter, MediaInfo, OcrResult, ProgressFn, Summary, Transcript
from . import deepseek

logger = logging.getLogger(__name__)

#: 不分块时转写证据的最大字符数
_EVIDENCE_MAX_CHARS = 14000
#: 分块时每块目标字符数 / 最大块数
_CHUNK_TARGET_CHARS = 9000
_CHUNK_MAX = 20
#: OCR 证据最多保留字符数
_OCR_EVIDENCE_LIMIT = 4000
#: 视觉描述最多条数
_VISUAL_NOTES_LIMIT = 10

_DEGRADED_NO_KEY = "（未配置 DeepSeek API Key，以下为转写节选）"

_SYSTEM_PROMPT = """你是专业的视频内容分析师。请严格依据用户提供的视频转写（带 [MM:SS] 时间戳）、画面文字（OCR）与元信息，输出一个 JSON 对象。

只输出 JSON，不要 markdown 代码块围栏，不要任何解释文字。

JSON 结构：
{
  "one_liner": "一句话结论（不超过 40 字）",
  "overview": "300-800 字中文概述，可用 markdown 小标题/列表",
  "topics": ["讨论主题 1", "讨论主题 2"],
  "chapters": [{"start": 0, "end": 65.2, "title": "章节标题（不超过 20 字）", "summary": "该章节一句话摘要"}],
  "key_points": ["关键要点（5-10 条）"],
  "keywords": ["关键词（5-10 个）"],
  "quotes": ["【MM:SS】带时间戳的原话（最多 5 条）"],
  "visual_notes": ["从画面文字补充的信息（可为空数组）"]
}

约束：
1. chapters 数量 5-12 个，按时间顺序排列，start < end，时间单位为秒且必须落在 [0, 视频总时长] 内；
2. 只使用材料中出现的信息，不要编造；材料没有提到的内容不要写；
3. 全部使用简体中文（专有名词、英文术语可保留原文）。"""

_MAP_PROMPT = """你是视频内容分析助手。下面是一段视频转写的一部分（带 [MM:SS] 时间戳）。
请用简体中文做要点式梳理：列出该片段的主要话题、关键信息（人物/事件/结论）以及值得引用的原话（保留 [MM:SS] 时间戳）。
要求：150-350 字，分点叙述，不要 JSON，不要编造材料以外的内容。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _emit(progress: Optional[ProgressFn], stage: str, pct: float, message: Optional[str]) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0.0, min(1.0, float(pct))), message)
    except Exception:  # pragma: no cover
        logger.debug("progress 回调异常", exc_info=True)


def _fmt_ts(seconds: float) -> str:
    """秒数 → ``MM:SS``。"""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    minutes, sec = divmod(total, 60)
    return f"{minutes:02d}:{sec:02d}"


def _fmt_duration(seconds: float) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {sec} 秒"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_text(value: Any, limit: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value if item is not None)
    text = str(value).strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit]
    return text


def _as_str_list(value: Any, *, limit: int = 10, item_limit: int = 300) -> List[str]:
    items: List[str] = []
    if value is None:
        return items
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return items
    for candidate in candidates:
        text = _as_text(candidate, limit=item_limit)
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


# ---------------------------------------------------------------------------
# 降级总结
# ---------------------------------------------------------------------------
def _degraded_summary(
    media: MediaInfo,
    transcript: Optional[Transcript],
    ocr: Optional[OcrResult],
    one_liner: str,
    visual_notes: Optional[List[str]] = None,
) -> Summary:
    transcript_text = (transcript.text or "").strip() if transcript else ""
    if transcript_text:
        overview = transcript_text[:2000]
    else:
        overview = "未能获取音频转写内容（视频可能没有音轨或为静音）。"
    notes: List[str] = []
    if ocr is not None:
        for frame in ocr.frames[:10]:
            text = (frame.text or "").strip()
            if text:
                notes.append(f"[{_fmt_ts(frame.time)}] {text[:160]}")
    if visual_notes:
        notes.extend(str(item)[:300] for item in visual_notes[:5] if str(item).strip())
    return Summary(
        title=(media.title or "").strip(),
        one_liner=one_liner,
        overview=overview,
        topics=[],
        chapters=[],
        key_points=[],
        keywords=[],
        quotes=[],
        visual_notes=notes,
        generated_by="",
        created_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# 材料拼装
# ---------------------------------------------------------------------------
def _transcript_lines(transcript: Transcript) -> List[str]:
    lines: List[str] = []
    for segment in transcript.segments or []:
        text = (segment.text or "").strip()
        if text:
            lines.append(f"[{_fmt_ts(segment.start)}] {text}")
    return lines


def _meta_block(media: MediaInfo, transcript: Optional[Transcript]) -> str:
    duration = float(getattr(media, "duration", 0.0) or 0.0)
    if duration <= 0 and transcript is not None:
        duration = float(transcript.duration or 0.0)
    parts = [f"视频标题：{(media.title or '未命名视频').strip()}"]
    if duration > 0:
        parts.append(f"视频时长：{_fmt_duration(duration)}（{duration:.1f} 秒）")
    if media.uploader:
        parts.append(f"上传者：{media.uploader}")
    if media.description:
        parts.append(f"视频简介：{str(media.description)[:800]}")
    return "\n".join(parts)


def _build_user_prompt(
    media: MediaInfo,
    transcript: Optional[Transcript],
    ocr: Optional[OcrResult],
    evidence: str,
    visual_notes: Optional[List[str]],
) -> str:
    parts = [_meta_block(media, transcript)]
    parts.append("\n===== 视频材料开始 =====")
    parts.append(evidence)
    ocr_text = (ocr.text or "").strip() if ocr else ""
    if ocr_text:
        parts.append("\n===== 画面文字（OCR，带时间戳）=====")
        parts.append(ocr_text[:_OCR_EVIDENCE_LIMIT])
    if visual_notes:
        parts.append("\n===== 画面视觉描述 =====")
        parts.append("\n".join(str(item)[:300] for item in visual_notes[:_VISUAL_NOTES_LIMIT]))
    parts.append("\n===== 视频材料结束 =====")
    parts.append("\n请根据以上材料输出规定的 JSON 对象：")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 长转写分块（map）
# ---------------------------------------------------------------------------
def _split_into_chunks(lines: List[str]) -> List[List[str]]:
    total_chars = sum(len(line) + 1 for line in lines)
    target = max(3000, min(_CHUNK_TARGET_CHARS, math.ceil(total_chars / _CHUNK_MAX)))
    chunks: List[List[str]] = []
    current: List[str] = []
    current_chars = 0
    for line in lines:
        length = len(line) + 1
        if current and current_chars + length > target:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += length
    if current:
        chunks.append(current)
    return chunks


def _map_chunks(
    lines: List[str],
    media: MediaInfo,
    transcript: Transcript,
    settings,
    model: str,
    progress: Optional[ProgressFn],
) -> List[str]:
    chunks = _split_into_chunks(lines)
    total = len(chunks)
    summaries: List[str] = []
    for index, chunk in enumerate(chunks):
        start_ts = chunk[0].split("]", 1)[0].lstrip("[") if chunk else ""
        end_ts = chunk[-1].split("]", 1)[0].lstrip("[") if chunk else ""
        chunk_text = "\n".join(chunk)
        messages = [
            {"role": "system", "content": _MAP_PROMPT},
            {
                "role": "user",
                "content": (
                    f"视频标题：{(media.title or '未命名视频').strip()}\n"
                    f"这是第 {index + 1}/{total} 部分（时间范围约 {start_ts} - {end_ts}）：\n\n"
                    f"{chunk_text}"
                ),
            },
        ]
        try:
            text = deepseek.chat(
                settings,
                messages,
                model=model,
                temperature=0.3,
                max_tokens=1200,
            )
            summary_text = (text or "").strip()
        except deepseek.DeepSeekError as exc:
            logger.warning("分段摘要失败（第 %s/%s 段）：%s", index + 1, total, exc)
            summary_text = ""
        if not summary_text:
            summary_text = "（该段摘要生成失败，节选：）" + chunk_text[:300]
        summaries.append(f"【第 {index + 1} 段 {start_ts}-{end_ts}】\n{summary_text}")
        _emit(
            progress,
            "analyzing",
            0.05 + 0.6 * (index + 1) / max(1, total),
            f"分段摘要 {index + 1}/{total}",
        )
    return summaries


# ---------------------------------------------------------------------------
# 章节校验
# ---------------------------------------------------------------------------
def _even_chapters(duration: float, transcript: Optional[Transcript], count: int = 5) -> List[Chapter]:
    if duration <= 0:
        return []
    count = max(1, min(int(count), 12))
    chapters: List[Chapter] = []
    segments = list(transcript.segments) if transcript else []
    for index in range(count):
        start = duration * index / count
        end = duration * (index + 1) / count
        excerpt = ""
        for segment in segments:
            if segment.start >= start and segment.start < end and (segment.text or "").strip():
                excerpt = segment.text.strip()[:60]
                break
        chapters.append(
            Chapter(
                start=round(start, 2),
                end=round(end, 2),
                title=f"第 {index + 1} 部分",
                summary=excerpt or f"视频第 {index + 1} 段内容。",
            )
        )
    return chapters


def _coerce_chapters(
    raw: Any,
    duration: float,
    transcript: Optional[Transcript],
) -> List[Chapter]:
    entries: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start"))
                end = float(item.get("end"))
            except (TypeError, ValueError):
                start = end = None
            entries.append(
                {
                    "start": start,
                    "end": end,
                    "title": _as_text(item.get("title"), limit=20),
                    "summary": _as_text(item.get("summary"), limit=300),
                }
            )
    if len(entries) > 12:
        entries = entries[:12]

    repaired: List[Chapter] = []
    total_entries = len(entries)
    for index, entry in enumerate(entries):
        start, end = entry["start"], entry["end"]
        valid = (
            start is not None
            and end is not None
            and math.isfinite(start)
            and math.isfinite(end)
            and end > start
        )
        if valid and duration > 0:
            start = min(max(start, 0.0), duration)
            end = min(max(end, 0.0), duration)
            valid = end > start
        if not valid:
            if duration <= 0:
                continue
            start = duration * index / total_entries
            end = duration * (index + 1) / total_entries
            if end <= start:
                continue
        repaired.append(
            Chapter(
                start=round(float(start), 2),
                end=round(float(end), 2),
                title=entry["title"] or f"第 {index + 1} 部分",
                summary=entry["summary"] or "（无摘要）",
            )
        )

    if not repaired:
        repaired = _even_chapters(duration, transcript)
    repaired.sort(key=lambda chapter: chapter.start)
    return repaired


# ---------------------------------------------------------------------------
# 结果规整
# ---------------------------------------------------------------------------
def _coerce_summary(
    data: Dict[str, Any],
    media: MediaInfo,
    transcript: Transcript,
    ocr: Optional[OcrResult],
    model: str,
    visual_notes: Optional[List[str]],
) -> Summary:
    duration = float(media.duration or transcript.duration or 0.0)
    one_liner = _as_text(data.get("one_liner"), limit=60)
    if len(one_liner) > 40:
        one_liner = one_liner[:40]
    if not one_liner:
        one_liner = _as_text(data.get("overview"), limit=40) or (media.title or "视频内容总结")[:40]
    overview = _as_text(data.get("overview")) or _as_text(data.get("summary"))
    chapters = _coerce_chapters(data.get("chapters"), duration, transcript)
    notes = _as_str_list(data.get("visual_notes"), limit=_VISUAL_NOTES_LIMIT, item_limit=300)
    if not notes and visual_notes:
        notes = [str(item)[:300] for item in visual_notes[:_VISUAL_NOTES_LIMIT] if str(item).strip()]
    return Summary(
        title=(media.title or "").strip(),
        one_liner=one_liner,
        overview=overview,
        topics=_as_str_list(data.get("topics"), limit=10, item_limit=100),
        chapters=chapters,
        key_points=_as_str_list(data.get("key_points"), limit=10, item_limit=300),
        keywords=_as_str_list(data.get("keywords"), limit=12, item_limit=50),
        quotes=_as_str_list(data.get("quotes"), limit=5, item_limit=300),
        visual_notes=notes,
        generated_by=model,
        created_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def summarize(
    media: MediaInfo,
    transcript: Transcript,
    ocr: OcrResult,
    settings,
    *,
    visual_notes: Optional[List[str]] = None,
    progress: Optional[ProgressFn] = None,
) -> Summary:
    """生成结构化 :class:`Summary`；无 Key / 调用失败时降级，不抛错。"""
    _emit(progress, "analyzing", 0.02, "准备总结材料…")
    if not deepseek.is_configured(settings):
        logger.info("未配置 DeepSeek API Key，生成降级总结（转写节选）")
        summary = _degraded_summary(media, transcript, ocr, _DEGRADED_NO_KEY, visual_notes)
        _emit(progress, "analyzing", 1.0, "未配置 API Key，已生成转写节选")
        return summary

    try:
        summary = _summarize_with_llm(
            media,
            transcript,
            ocr,
            settings,
            visual_notes or [],
            progress,
        )
        _emit(progress, "analyzing", 1.0, "内容总结完成")
        return summary
    except deepseek.DeepSeekError as exc:
        logger.warning("DeepSeek 总结失败，降级为转写节选：%s", exc)
        summary = _degraded_summary(
            media,
            transcript,
            ocr,
            f"（DeepSeek 总结失败：{exc}以下为转写节选）"[:200],
            visual_notes,
        )
    except Exception as exc:  # pragma: no cover - 兜底，保证作业不中断
        logger.exception("生成总结时出现异常，降级为转写节选")
        summary = _degraded_summary(
            media,
            transcript,
            ocr,
            f"（总结生成失败：{exc}）",
            visual_notes,
        )
    _emit(progress, "analyzing", 1.0, "总结降级为转写节选")
    return summary


def _summarize_with_llm(
    media: MediaInfo,
    transcript: Transcript,
    ocr: OcrResult,
    settings,
    visual_notes: List[str],
    progress: Optional[ProgressFn],
) -> Summary:
    model = str(getattr(settings, "resolved_summary_model", "") or "deepseek-chat").strip()
    lines = _transcript_lines(transcript)
    ocr_text = (ocr.text or "").strip() if ocr else ""
    if not lines and not ocr_text and not visual_notes:
        logger.info("转写与画面文字均为空，无法生成智能总结")
        return _degraded_summary(
            media,
            transcript,
            ocr,
            "（视频没有可用的转写或画面文字内容，无法生成总结）",
            visual_notes,
        )

    total_chars = sum(len(line) + 1 for line in lines)
    if total_chars <= _EVIDENCE_MAX_CHARS:
        evidence = "以下是视频转写（带时间戳）：\n" + "\n".join(lines) if lines else "（无音频转写内容）"
        _emit(progress, "analyzing", 0.2, "生成内容总结…")
    else:
        _emit(progress, "analyzing", 0.05, f"转写较长（{total_chars} 字），先分段摘要…")
        chunk_summaries = _map_chunks(lines, media, transcript, settings, model, progress)
        evidence = (
            "视频转写较长，以下是按时间顺序的分段摘要（含时间范围）：\n\n"
            + "\n\n".join(chunk_summaries)
        )
        _emit(progress, "analyzing", 0.7, "合并分段摘要，生成总结…")

    user_prompt = _build_user_prompt(media, transcript, ocr, evidence, visual_notes)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    _emit(progress, "analyzing", 0.8, "请求 DeepSeek 生成结构化总结…")
    data = deepseek.chat_json(settings, messages, model=model, max_tokens=6000)
    return _coerce_summary(data, media, transcript, ocr, model, visual_notes)
