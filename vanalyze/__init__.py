"""视频分析 · DeepSeek —— 本地视频/网页视频 转写、总结与问答。

核心能力：
- 本地 Whisper（faster-whisper，RTX GPU 加速）音频转文字
- 关键帧抽取 + 本地 OCR 读取画面文字（DeepSeek 无视觉 API）
- DeepSeek 生成结构化总结
- 基于转写内容的检索问答（DeepSeek）
"""

__version__ = "1.0.0"
