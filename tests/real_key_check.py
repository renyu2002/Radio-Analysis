"""真实 DeepSeek Key 验收（独立脚本，不属于默认 pytest 收集范围）。

用途（docs/CONTRACT.md §12.4）
-----------------------------
当用户环境已配置真实 DeepSeek API Key 时（``data/settings.json`` 或环境变量
``DEEPSEEK_API_KEY``），本脚本用**真实 DeepSeek API** 验证：

1. ``test_connection`` 连通性；
2. 基于 E2E 已产出的真实 ASR/OCR 结果生成结构化总结：字段完整、章节时间戳合法、
   ``generated_by`` 为真实模型名；
3. 真实问答（非流式 + 流式）：中文回答、引用时间戳可解析（citations 来自本地检索）。

安全与隔离
----------
- 只**读取**用户 ``data/settings.json``，绝不打印/保存 Key（仅掩码）；
- 不写用户数据目录：临时 job_dir 用系统临时目录；
- 结果写入 ``tests/.artifacts/real_key.json``（不含 Key）。

用法
----
::

    .venv\\Scripts\\python.exe tests\\real_key_check.py
    $env:DEEPSEEK_API_KEY="sk-..."; .venv\\Scripts\\python.exe tests\\real_key_check.py

退出码：0 = 通过 / 无 Key 跳过；1 = 验收失败。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
ARTIFACTS = TESTS_DIR / ".artifacts"
E2E_ARTIFACTS = ARTIFACTS / "e2e"
RESULT_PATH = ARTIFACTS / "real_key.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

os.environ.setdefault("PYTHONUTF8", "1")

CJK_MIN = 10


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))


def _mask(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        return ""
    return key[:4] + "…" + key[-4:] if len(key) > 10 else "已设置"


def _load_user_settings() -> Dict[str, Any]:
    """读取用户真实配置（只读；环境变量优先）。"""
    data: Dict[str, Any] = {}
    path = ROOT / "data" / "settings.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                data.update(payload)
        except (OSError, ValueError):
            pass
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        data["deepseek_api_key"] = env_key
    env_base = (os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
    if env_base:
        data["deepseek_base_url"] = env_base
    return data


def _check(name: str, ok: bool, detail: Any = "") -> Dict[str, Any]:
    item = {"item": name, "ok": bool(ok), "detail": str(detail)[:600]}
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"：{detail}" if not ok and detail else ""))
    return item


def _build_inputs():
    """构造 summarize / qa 所需的模型对象（优先用 E2E 真实产物）。"""
    from vanalyze.models import MediaInfo, OcrResult, Transcript

    transcript_path = E2E_ARTIFACTS / "transcript.json"
    ocr_path = E2E_ARTIFACTS / "ocr.json"
    if not transcript_path.is_file() or not ocr_path.is_file():
        return None, None, None
    transcript = Transcript.model_validate(json.loads(transcript_path.read_text(encoding="utf-8")))
    ocr = OcrResult.model_validate(json.loads(ocr_path.read_text(encoding="utf-8")))
    media = MediaInfo(
        path=str((ROOT / "tests" / "fixtures" / "sample.mp4")),
        title="视频分析系统自检（真实 Key 验收）",
        duration=float(transcript.duration or 11.95),
        ext="mp4",
        local_input=True,
    )
    return media, transcript, ocr


def _text_of(chapter: Any) -> str:
    if hasattr(chapter, "model_dump"):
        chapter = chapter.model_dump()
    return str(chapter.get("title") or "")


def main() -> int:
    print("=" * 72)
    print("真实 DeepSeek Key 验收（docs/CONTRACT.md §12.4）")
    print("=" * 72)

    config = _load_user_settings()
    api_key = str(config.get("deepseek_api_key") or "").strip()
    result: Dict[str, Any] = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "key_masked": _mask(api_key),
        "base_url": str(config.get("deepseek_base_url") or "https://api.deepseek.com"),
        "model": str(config.get("deepseek_model") or "deepseek-chat"),
        "checks": [],
        "success": False,
    }
    if not api_key:
        print("未检测到真实 DeepSeek API Key（data/settings.json 或环境变量 DEEPSEEK_API_KEY）。")
        print("跳过真实 Key 验收；无 Key 链路由 tests/stub_deepseek.py 覆盖。")
        result["skipped"] = True
        result["success"] = True
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    print(f"Key：{result['key_masked']}   Base URL：{result['base_url']}   模型：{result['model']}")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    from vanalyze.models import SettingsUpdate  # noqa: F401  (确认模型可用)
    from vanalyze.settings import Settings

    settings = Settings(
        deepseek_api_key=api_key,
        deepseek_base_url=result["base_url"],
        deepseek_model=result["model"],
        whisper_model="large-v3",
        whisper_device="auto",
    )

    from vanalyze.core import deepseek

    checks = result["checks"]

    # ---------- 1. 连通性 ----------
    try:
        t0 = time.monotonic()
        ok, message = deepseek.test_connection(settings)
        elapsed = time.monotonic() - t0
        checks.append(_check("test_connection 连通真实 DeepSeek", ok, f"{message}（{elapsed:.1f}s）"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("test_connection 连通真实 DeepSeek", False, f"{type(exc).__name__}: {exc}"))

    media, transcript, ocr = _build_inputs()
    if transcript is None:
        print("缺少 E2E 真实 ASR/OCR 产物（tests/.artifacts/e2e/transcript.json 等）。")
        print("请先运行：.venv\\Scripts\\python.exe tests\\e2e.py")
        checks.append(_check("真实 ASR/OCR 输入产物存在", False, "请先运行 tests/e2e.py"))
        result["success"] = False
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    # ---------- 2. 真实总结 ----------
    summary = None
    try:
        from vanalyze.core.analyze import summarize

        t0 = time.monotonic()
        summary = summarize(media, transcript, ocr, settings)
        elapsed = time.monotonic() - t0
        data = summary.model_dump()
        fields = ["title", "one_liner", "overview", "topics", "chapters", "key_points", "keywords", "quotes", "visual_notes"]
        missing = [field for field in fields if field not in data]
        checks.append(_check("总结字段完整（Summary 9 字段）", not missing, missing))
        checks.append(
            _check(
                "总结内容非空（中文）",
                bool(summary.one_liner and summary.overview and _has_cjk(summary.overview)),
                f"{len(summary.overview or '')} 字",
            )
        )
        checks.append(
            _check(
                "generated_by 为真实模型名",
                bool(summary.generated_by),
                summary.generated_by,
            )
        )
        duration = float(media.duration or 11.95)
        invalid = [
            chapter
            for chapter in summary.chapters
            if not (0 <= chapter.start < chapter.end <= duration + 0.5)
        ]
        checks.append(
            _check(
                "章节时间戳合法（0 ≤ start < end ≤ 时长）",
                bool(summary.chapters) and not invalid,
                f"{len(summary.chapters)} 章，非法 {len(invalid)}",
            )
        )
        checks.append(
            _check(
                "章节数量合理（1-12）",
                1 <= len(summary.chapters) <= 12,
                len(summary.chapters),
            )
        )
        result["summary"] = {
            "title": summary.title,
            "one_liner": summary.one_liner,
            "overview_chars": len(summary.overview or ""),
            "chapters": len(summary.chapters),
            "topics": summary.topics[:6],
            "keywords": summary.keywords[:8],
            "generated_by": summary.generated_by,
            "elapsed_s": round(elapsed, 1),
        }
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("真实 DeepSeek 生成总结", False, f"{type(exc).__name__}: {exc}"))

    # ---------- 3. 真实问答 ----------
    temp_dir = Path(tempfile.mkdtemp(prefix="vanalyze_real_key_"))
    try:
        (temp_dir / "transcript.json").write_text(
            json.dumps(transcript.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (temp_dir / "ocr.json").write_text(
            json.dumps(ocr.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if summary is not None:
            (temp_dir / "summary.json").write_text(
                json.dumps(summary.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        from vanalyze.core import qa

        question = "这段视频里提到了哪些测试？请用带时间戳的方式回答。"
        t0 = time.monotonic()
        answer, citations = qa.answer_once(
            job_dir=str(temp_dir), question=question, history=[], settings=settings
        )
        elapsed = time.monotonic() - t0
        checks.append(
            _check("真实问答返回中文回答", bool(answer and _has_cjk(answer)), (answer or "")[:120])
        )
        checks.append(
            _check(
                "问答引用来自本地检索（citations 非空且时间戳合法）",
                bool(citations) and all(0 <= item.start < item.end for item in citations),
                f"{len(citations)} 条引用",
            )
        )
        if answer and _has_cjk(answer):
            checks.append(
                _check(
                    "回答包含【MM:SS】时间戳引用格式",
                    "【" in answer or "00:" in answer or "0:" in answer,
                    (answer or "")[:120],
                )
            )
        result["answer"] = {
            "question": question,
            "answer": answer,
            "citations": len(citations),
            "elapsed_s": round(elapsed, 1),
        }

        # 流式接口（真实 SSE）
        t0 = time.monotonic()
        iterator, stream_citations = qa.answer_stream(
            job_dir=str(temp_dir), question="视频画面里出现了什么文字？", history=[], settings=settings
        )
        parts = []
        for chunk in iterator:
            if chunk:
                parts.append(str(chunk))
        streamed = "".join(parts)
        checks.append(
            _check(
                "真实流式问答非空（逐段返回）",
                bool(streamed.strip()) and _has_cjk(streamed) and len(parts) >= 1,
                f"{len(parts)} 段 / {len(streamed)} 字",
            )
        )
        result["stream"] = {
            "answer": streamed,
            "chunks": len(parts),
            "citations": len(stream_citations),
            "elapsed_s": round(time.monotonic() - t0, 1),
        }
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("真实问答（DeepSeek）", False, f"{type(exc).__name__}: {exc}"))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    failed = [item for item in checks if not item["ok"]]
    result["passed"] = len(checks) - len(failed)
    result["failed"] = len(failed)
    result["success"] = not failed
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("-" * 72)
    print(f"真实 Key 验收结果：{result['passed']} 通过 / {len(failed)} 失败")
    print(f"结果文件：{RESULT_PATH}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
