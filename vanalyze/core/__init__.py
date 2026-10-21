"""视频分析核心模块（core-dev）。

包含：
- :mod:`vanalyze.core.ingest` 视频来源解析与下载（本地文件 / 网页链接 / 标签页）
- :mod:`vanalyze.core.asr` 本地 faster-whisper 语音转文字（CUDA 优先、CPU 降级）
- :mod:`vanalyze.core.vision` 关键帧抽取 + RapidOCR 画面文字识别
- :mod:`vanalyze.core.deepseek` DeepSeek（OpenAI 兼容）客户端
- :mod:`vanalyze.core.analyze` 结构化视频总结
- :mod:`vanalyze.core.qa` 基于转写的检索问答
- :mod:`vanalyze.core.pipeline` 完整流水线与总结重算

对外接口（契约 §6.1）：
``run_pipeline`` / ``PipelineError`` / ``regenerate_summary``。
"""

from __future__ import annotations

from .pipeline import PipelineError, regenerate_summary, run_pipeline

__all__ = [
    "run_pipeline",
    "PipelineError",
    "regenerate_summary",
]
