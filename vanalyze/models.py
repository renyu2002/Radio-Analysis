"""共享数据模型（前后端/core 之间的契约）。

所有跨模块数据结构都在这里定义，core / server / web 均以此为准。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# 进度回调： stage_key, progress(0..1), message
ProgressFn = Callable[[str, float, Optional[str]], None]

JobStatus = Literal["pending", "running", "done", "error"]
SourceType = Literal["file", "url", "tab"]
StageStatus = Literal["pending", "running", "done", "error"]


class StageState(BaseModel):
    key: str
    label: str
    status: StageStatus = "pending"
    progress: float = 0.0


class MediaInfo(BaseModel):
    """已解析、可本地读取的媒体文件信息。"""

    path: str = ""                     # 本地绝对路径
    source_type: SourceType = "file"
    source_url: Optional[str] = None   # 用户给的原始链接
    webpage_url: Optional[str] = None  # 页面链接（tab/网页来源）
    title: str = ""
    duration: float = 0.0              # 秒
    uploader: Optional[str] = None
    description: Optional[str] = None
    thumbnail_path: Optional[str] = None
    ext: Optional[str] = None
    local_input: bool = False          # 是否用户本机文件（不复制、不删除）
    payload: Dict[str, Any] = Field(default_factory=dict)


class TranscriptSegment(BaseModel):
    id: int
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    language: str = "zh"
    duration: float = 0.0
    segments: List[TranscriptSegment] = Field(default_factory=list)
    text: str = ""


class OcrFrame(BaseModel):
    time: float
    text: str
    frame: Optional[str] = None   # 相对 frames/ 的文件名
    score: float = 0.0


class OcrResult(BaseModel):
    frames: List[OcrFrame] = Field(default_factory=list)
    text: str = ""


class Chapter(BaseModel):
    start: float
    end: float
    title: str
    summary: str


class Summary(BaseModel):
    title: str = ""
    one_liner: str = ""
    overview: str = ""                       # markdown
    topics: List[str] = Field(default_factory=list)
    chapters: List[Chapter] = Field(default_factory=list)
    key_points: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    quotes: List[str] = Field(default_factory=list)
    visual_notes: List[str] = Field(default_factory=list)
    generated_by: str = ""
    created_at: str = ""


class Job(BaseModel):
    id: str
    status: JobStatus = "pending"
    stage: str = "queued"
    stage_label: str = "排队中"
    progress: float = 0.0
    source_type: SourceType = "file"
    title: str = ""
    source_url: Optional[str] = None
    duration: float = 0.0
    thumbnail_url: Optional[str] = None
    media_url: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    error: Optional[str] = None
    stages: List[StageState] = Field(default_factory=list)
    has_transcript: bool = False
    has_summary: bool = False
    has_ocr: bool = False
    segment_count: int = 0
    message_count: int = 0
    summary: Optional[Summary] = None


class Citation(BaseModel):
    segment_id: int = 0
    start: float = 0.0
    end: float = 0.0
    text: str = ""


class ChatMessage(BaseModel):
    id: int = 0
    role: Literal["user", "assistant"] = "user"
    content: str = ""
    created_at: str = ""
    citations: List[Citation] = Field(default_factory=list)


class ChatRequest(BaseModel):
    question: str
    stream: bool = True


class ChatResponse(BaseModel):
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    message: Optional[ChatMessage] = None


class TabIngestRequest(BaseModel):
    """Chrome 插件上报的当前标签页视频信息。"""

    page_url: str = ""
    video_url: Optional[str] = None  # video.currentSrc（可能是 blob:）
    title: str = ""
    duration: float = 0.0
    poster: Optional[str] = None


class UrlIngestRequest(BaseModel):
    source_type: Literal["url"] = "url"
    url: str


class SettingsView(BaseModel):
    """GET /api/settings 返回（密钥只回显掩码）。"""

    deepseek_api_key_set: bool = False
    deepseek_api_key_masked: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_reasoner_model: str = "deepseek-reasoner"
    summary_model: str = ""
    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    whisper_language: str = "auto"
    max_frames: int = 100
    frame_interval: float = 20.0
    vision_enabled: bool = False
    vision_base_url: str = ""
    vision_model: str = ""
    vision_api_key_set: bool = False
    keep_media: bool = True
    port: int = 8765
    data_dir: str = ""


class SettingsUpdate(BaseModel):
    """PATCH/PUT /api/settings 请求体（未提供的字段保持不变）。"""

    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None
    deepseek_reasoner_model: Optional[str] = None
    summary_model: Optional[str] = None
    whisper_model: Optional[str] = None
    whisper_device: Optional[str] = None
    whisper_compute_type: Optional[str] = None
    whisper_language: Optional[str] = None
    max_frames: Optional[int] = None
    frame_interval: Optional[float] = None
    vision_enabled: Optional[bool] = None
    vision_base_url: Optional[str] = None
    vision_model: Optional[str] = None
    vision_api_key: Optional[str] = None
    keep_media: Optional[bool] = None
