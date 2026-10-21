# 视频分析 · DeepSeek —— 接口契约（唯一事实来源）

> 本文件由 Lead 维护。所有实现必须严格遵循本契约；如发现契约缺陷，**先给 Lead 发消息**，不要擅自改变跨模块接口。
> 项目根目录：`<PROJECT_ROOT>`（下称 `ROOT`）。

## 1. 产品目标

本地网页应用：输入 **本地视频 / 网页链接（B站、YouTube 等）/ 浏览器当前标签页正在播放的视频**，完成：

1. **音频转文字**：本地 faster-whisper（RTX 4080，CUDA，离线，中文）；
2. **视频内容理解与总结**：DeepSeek 基于转写 + 画面文字（本地 OCR，因 DeepSeek 无视觉 API）生成结构化总结；
3. **基于视频内容的问答**：本地检索 + DeepSeek 流式回答，答案带时间戳引用。

用户已有 DeepSeek API Key（在网页「设置」里填写，或环境变量 `DEEPSEEK_API_KEY`）。所有 LLM 调用默认走 DeepSeek（`https://api.deepseek.com`，OpenAI 兼容）；视觉模型为可选扩展（默认关闭）。

## 2. 技术栈与运行方式

- Python 3.12 + venv：`ROOT\.venv\Scripts\python.exe`（依赖已装好，别再改 `requirements.txt`，缺包找 Lead）
- 后端：FastAPI + uvicorn，监听 `127.0.0.1:8765`
- 前端：`web/` 纯静态 SPA（原生 HTML/CSS/JS，无构建步骤），由后端挂载在 `/`
- 浏览器扩展：`extension/` Chrome MV3
- 存储：`data/app.db`（SQLite）+ `data/jobs/<job_id>/`（媒体与产物）+ `data/settings.json`
- 启动：`ROOT\run.ps1`（Lead 编写）
- 编码：所有文本 UTF-8；Windows 控制台输出需 `PYTHONUTF8=1`（run.ps1 里设置）

## 3. 目录与文件所有权

| 路径 | 所有者 | 说明 |
|---|---|---|
| `vanalyze/config.py`, `models.py`, `settings.py`, `__init__.py` | Lead | 已写好，**禁止修改**；需要扩展先找 Lead |
| `vanalyze/core/**` | core-dev | 采集 / ASR / 帧+OCR / DeepSeek 客户端 / 总结 / 问答 |
| `vanalyze/store.py`, `vanalyze/server/**`, `vanalyze/__main__.py` | server-dev | SQLite、作业管理、HTTP API、静态托管、入口 |
| `web/**` | ui-dev | 前端 SPA |
| `extension/**` | ext-dev | Chrome 扩展 |
| `tests/**`, `docs/VERIFICATION.md` | verifier | 测试与验收 |
| `docs/CONTRACT.md`, `README.md`, `requirements.txt`, `run.ps1`, `setup.ps1`, `.gitignore` | Lead | 集成与文档 |
| `data/**` | 运行时 | 不入库 |

**写文件纪律**：只写自己 scope 内的文件；不要 `git init/commit`；不要删除别人文件；不要改 `.venv`；不要重启别人的服务（端口占用见 §9）。

## 4. 共享数据模型

全部定义在 `vanalyze/models.py`（**只读**）：`MediaInfo, Transcript, TranscriptSegment, OcrFrame, OcrResult, Summary, Chapter, Job, StageState, ChatMessage, Citation, ChatRequest, ChatResponse, TabIngestRequest, UrlIngestRequest, SettingsView, SettingsUpdate, ProgressFn`。
配置在 `vanalyze/settings.py`：`Settings`, `load_settings()`, `save_settings()`, `update_settings(SettingsUpdate)`, `get_api_key()`。
路径常量在 `vanalyze/config.py`：`DATA_DIR, JOBS_DIR, DB_PATH, SETTINGS_PATH, WEB_DIR, VERSION, DEFAULT_PORT=8765`。

## 5. 作业目录产物（job_dir = `data/jobs/<job_id>/`）

| 文件 | 产生者 | 内容 |
|---|---|---|
| `source.<ext>` | core.ingest | 下载/上传的媒体文件；本地文件则不复制 |
| `media.json` | core.pipeline | `MediaInfo` JSON（`path` 必须是可读的本地绝对路径） |
| `thumbnail.jpg` | core.ingest | 视频缩略图（可缺失） |
| `transcript.json` | core.asr | `Transcript` |
| `frames/f0001.jpg` … | core.vision | 关键帧（JPEG，宽 ≤1280） |
| `ocr.json` | core.vision | `OcrResult`（`frame` 为 `frames/xxx.jpg` 文件名） |
| `summary.json` | core.analyze | `Summary`；无 Key 时也写一个降级版（`generated_by=""`，`overview` 放转写节选），不阻断作业 |

所有 JSON 用 `model_dump_json(indent=2, ensure_ascii=False)` 写入，UTF-8。

## 6. core 接口（core-dev 实现，签名不可改）

### 6.1 `vanalyze/core/__init__.py`
导出：`run_pipeline`, `PipelineError`, `regenerate_summary`。

### 6.2 `vanalyze/core/pipeline.py`
```python
class PipelineError(Exception): ...   # str 为可直接展示给用户的中文

def run_pipeline(
    *, job_id: str, job_dir: str, source: dict, settings: Settings,
    progress: ProgressFn | None = None,
) -> None:
    """顺序执行：resolve_source → probe → thumbnail → transcribe → extract_frames → ocr
    → summarize；产物写 job_dir。失败抛 PipelineError（中文消息）。
    progress(stage_key, pct_0..1, message) 用于上报；stage_key 见 §7。
    OCR/关键帧失败可降级（写空 ocr.json，继续）；ASR 失败必须抛错。
    """

def regenerate_summary(*, job_dir: str, settings: Settings, progress: ProgressFn | None = None) -> Summary:
    """读取 job_dir 已有产物重新生成 summary.json 并返回。"""
```

### 6.3 `vanalyze/core/ingest.py`
```python
def resolve_source(source: dict, *, dest_dir: str, progress: ProgressFn | None = None) -> MediaInfo: ...
def probe_media(path: str) -> MediaInfo: ...          # 至少填 path/duration/title/ext
def make_thumbnail(media_path: str, out_path: str, at: float | None = None) -> bool: ...
```
`source` 结构：
```python
{"source_type": "file", "file_path": r"C:\...\a.mp4"}
{"source_type": "url",  "url": "https://www.bilibili.com/video/BVxxx"}
{"source_type": "tab",  "page_url": "...", "video_url": "...", "title": "..."}   # video_url 可能是 blob:
```
规则：
- `file`：验证存在/可读，`MediaInfo(local_input=True, path=原绝对路径)`。
- `url`/`tab`：先用 yt-dlp（Python API）尝试页面链接；支持时下载 `bestvideo*+bestaudio/best`，ffmpeg 位置取 `imageio_ffmpeg.get_ffmpeg_exe()`，产物必须为标准可解码文件（mp4/webm/mkv 均可）；取 title/duration/uploader/thumbnail。
- `tab` 且 `video_url` 是 `http(s)` 直链：直接流式下载（带进度）作为兜底。
- 都失败时抛 `PipelineError`，消息示例：「无法解析该网页的视频。该页面可能是 blob/DRM 受保护的流媒体，请改用『下载视频后拖入网页』或换用视频页面链接。」
- 下载显示百分比进度（`downloading`, 0..1）。yt-dlp 的进度钩子需转换为 0..1。
- 网络请求默认超时 30s，重试 1 次；B站/YouTube 需要合并音视频，ffmpeg 不可用时降级单文件流 `best`。

### 6.4 `vanalyze/core/asr.py`
```python
def transcribe(media_path: str, settings: Settings, *, duration: float = 0.0,
               progress: ProgressFn | None = None) -> Transcript: ...
def detect_device(settings: Settings) -> tuple[str, str]:  # ("cuda","float16") / ("cpu","int8")
```
- faster-whisper，模型 `settings.whisper_model`（默认 `large-v3`），`device` 按 `settings.whisper_device`（auto → 有可用 CUDA 用 cuda/float16，否则 cpu/int8），模型实例进程内缓存（单例）。
- `vad_filter=True`，`language=None`（auto，若 settings 非 auto 则传），`beam_size=5`，`condition_on_previous_text=False`，`initial_prompt="以下是普通话视频的音频转写，请使用简体中文和规范标点。"`
- 进度按 `segment.end / duration` 更新（`transcribing`），每 20 句可回调一次。
- 组装 `Transcript.text` 为全部文本（换行分隔），`segments[i].id = i`（从 1 开始）。
- GPU 不可用/缺 CUDA 库时自动降级 CPU 并在日志打印警告，**不能崩溃**。CUDA 依赖 DLL 的加载逻辑放 `vanalyze/core/gpu.py`（可 `os.add_dll_directory` 指向 venv 的 `nvidia/cudnn/bin`、`nvidia/cublas/bin`）。
- 模型下载：HuggingFace 默认；若失败可提示设置 `HF_ENDPOINT=https://hf-mirror.com`（不要硬编码）。
- 空音频/静音：返回空 segments，不抛错（总结再降级）。

### 6.5 `vanalyze/core/vision.py`
```python
def extract_frames(media_path: str, out_dir: str, *, max_frames: int, interval: float,
                   duration: float = 0.0, progress: ProgressFn | None = None) -> list[tuple[float, str]]:
    """返回 [(秒, 帧绝对路径)]；失败返回 []（不抛错）。"""
def run_ocr(frames: list[tuple[float, str]], *, progress: ProgressFn | None = None) -> OcrResult: ...
def describe_frames(frames: list[tuple[float, str]], settings: Settings,
                    *, progress: ProgressFn | None = None) -> list[str]: ...
```
- 抽帧：按 `interval`（默认 20s）均匀采样 + 首帧 + 场景变化帧；用 OpenCV 读取，缩放到宽 ≤1280，dHash/直方图去重；总数 ≤ `max_frames`；JPEG 质量 82；文件名 `f%04d.jpg`。
- OCR：`rapidocr`（若 `rapidocr_onnxruntime` 亦然，写兼容层）。逐帧识别，行文本用 ` / ` 连接；`score` 取平均置信度；单帧文本上限 800 字；`OcrResult.text` 汇总为 `[MM:SS] 文本` 行。识别阈值/模型用默认即可。
- `describe_frames`：仅当 `settings.vision_enabled` 且 `vision_base_url/model/api_key` 齐全时调用 OpenAI 兼容视觉接口（发送 3 帧拼图或逐帧），返回每帧一句中文描述；失败/未启用返回 `[]`，不抛错。**默认关闭。**
- OCR 模型在进程内缓存。

### 6.6 `vanalyze/core/deepseek.py`
```python
class DeepSeekError(Exception): ...   # str 为中文，可直接展示
def is_configured(settings: Settings) -> bool
def chat(settings, messages: list[dict], *, model: str | None = None, temperature: float = 0.3,
         max_tokens: int = 4000, json_mode: bool = False) -> str
def chat_stream(settings, messages: list[dict], *, model=None, temperature=0.3,
                max_tokens=4000) -> Iterator[str]
def chat_json(settings, messages: list[dict], *, model=None, max_tokens=4000) -> dict
def test_connection(settings) -> tuple[bool, str]
```
- 请求：`{base_url}/chat/completions`，`Authorization: Bearer`，httpx，超时 connect 15s / read 300s。
- 重试：429/5xx/超时重试 2 次（指数退避 1s/3s）。
- 401 → 「DeepSeek API Key 无效（401），请在设置中检查。」；无 Key → 「尚未配置 DeepSeek API Key，请在右上角「设置」中填写。」
- `stream=True` 解析 SSE `data:` 行，yield `choices[0].delta.content`；遇到 `[DONE]` 结束。
- `json_mode=True` 传 `response_format={"type":"json_object"}`；`chat_json` 负责剥 ```json 围栏、截取首尾大括号、失败重试一次，仍失败抛 `DeepSeekError`。
- 模型默认 `settings.resolved_summary_model`；`deepseek-reasoner` 不传 temperature/response_format（模型限制），内部自动处理。

### 6.7 `vanalyze/core/analyze.py`
```python
def summarize(media: MediaInfo, transcript: Transcript, ocr: OcrResult, settings: Settings,
              *, visual_notes: list[str] | None = None,
              progress: ProgressFn | None = None) -> Summary: ...
```
- 无 Key：返回降级 `Summary(title=media.title, one_liner="（未配置 DeepSeek API Key，以下为转写节选）", overview=前 2000 字转写, visual_notes=OCR 前若干行, generated_by="")`，不抛错。
- 有 Key：证据 = 元信息 + 带 `[MM:SS]` 时间戳的转写（长视频先分块做分段摘要再合并）+ 画面文字（建议保留前 4000 字）。
- 输出严格 JSON（`chat_json`），字段即 `Summary`：`one_liner`（≤40字）、`overview`（markdown，300-800字）、`topics`、`chapters`（5-12 个，时间戳必须落在 [0, duration]，`start<end`，标题≤20字）、`key_points`（5-10）、`keywords`、`quotes`（带时间戳的原话，最多 5 条）、`visual_notes`（画面文字带来的补充信息，可空）。
- 时间戳兜底校验：非法 chapter 用总时长均分修正；`generated_by = 实际模型名`，`created_at` 为本地时间 ISO。
- 进度：`analyzing` 0..1（分块时按块数）。

### 6.8 `vanalyze/core/qa.py`
```python
def answer_stream(*, job_dir: str, question: str, history: list[dict], settings: Settings
                  ) -> tuple[Iterator[str], list[Citation]]: ...
def answer_once(*, job_dir: str, question: str, history: list[dict], settings: Settings
                ) -> tuple[str, list[Citation]]: ...
```
- 读取 `transcript.json / summary.json / ocr.json`（job_dir 内）。
- 本地检索：中文字符 bigram + 英文词的 BM25，在 segments 上取 top-14，合并相邻段（±1），再拼上 chapters 摘要与 OCR 时间线（截断）；总上下文 ≤ 45000 字。
- `history` 为 `[{"role":"user"|"assistant","content":...}]`，只取最近 8 条，每条截断 2000 字。
- System Prompt（核心约束）：你是视频内容分析助手，只能依据提供的视频转写/画面文字/总结回答；回答用简体中文；引用证据时用 `【MM:SS】` 格式；资料没有就直说「视频中没有提到」，不要编造。
- 返回 citations：被检索到的 segments（`Citation(segment_id,start,end,text截断)`），供 UI 可点击跳转。
- 模型：`settings.deepseek_model`；`temperature=0.3`，`max_tokens=2000`。
- 流式接口先同步完成检索再返回 iterator（检索很快）。
- 无 Key 抛 `DeepSeekError`。

## 7. 进度阶段（stage_key → 中文标签）

| key | label | 触发 |
|---|---|---|
| `queued` | 排队中 | 刚创建 |
| `resolving` | 解析视频信息 | 开始 |
| `downloading` | 下载视频 | 网页来源 |
| `probing` | 读取媒体信息 | 本地文件/下载后 |
| `transcribing` | 语音转文字 | ASR |
| `frames` | 抽取画面关键帧 | 抽帧 |
| `ocr` | 识别画面文字 | OCR |
| `analyzing` | 生成内容总结 | DeepSeek |
| `done` | 完成 | 成功结束 |
| `error` | 出错 | 失败 |

## 8. HTTP API（server-dev 实现；**路径、字段名、状态码必须一致**）

Base：`http://127.0.0.1:8765`。所有响应 JSON，UTF-8。错误统一：
```json
{"detail": "中文错误消息"}
```
HTTP 400 参数错误 / 404 不存在 / 409 状态不允许 / 500 服务器错误 / 502 上游（DeepSeek/下载）错误。

### 8.1 `GET /api/health`
```json
{"ok": true, "version": "1.0.0", "deepseek_configured": true,
 "asr": {"device": "cuda", "model": "large-v3", "compute_type": "float16", "cuda_available": true},
 "port": 8765}
```
快速返回，**不得加载 whisper 模型**。

### 8.2 `POST /api/jobs/upload`（multipart）
字段：`file`（必填，视频文件）、`title`（可选）。响应 200：`{"job": Job}`。
上传即创建作业并开始处理；本地文件直接使用，**不复制**（上传时保存到 `data/uploads/<uuid>_<name>` 再由 pipeline 引用，见 §9 归属）。

### 8.3 `POST /api/jobs`（JSON）
```json
{"source_type":"url","url":"https://www.bilibili.com/video/BVxxx"}
{"source_type":"tab","page_url":"https://...","video_url":"blob:https://...","title":"标题","duration":123.4,"poster":null}
```
响应 200：`{"job": Job}`。

### 8.4 `GET /api/jobs?limit=50`
```json
{"jobs": [Job, ...]}   // 按 created_at 倒序；Job 不含 summary 正文，但含 has_* / counts
```

### 8.5 `GET /api/jobs/{job_id}`
```json
{"job": Job}
```
`Job.summary` 仅在 `has_summary` 时内联返回（详情页一次拿全）。`Job.stages` 为全部阶段状态。`media_url` = `/api/jobs/{id}/media`，`thumbnail_url` = `/api/jobs/{id}/thumbnail`（无图则为 null）。

### 8.6 `GET /api/jobs/{job_id}/transcript`
```json
{"transcript": Transcript}
```
未完成 → 404 `{"detail":"转写尚未完成"}`。

### 8.7 `GET /api/jobs/{job_id}/summary` → `{"summary": Summary}`
### 8.8 `GET /api/jobs/{job_id}/ocr` → `{"ocr": OcrResult}`
### 8.9 `GET /api/jobs/{job_id}/media`：视频文件流，**必须支持 HTTP Range**（`206 Partial Content`，`Accept-Ranges: bytes`、`Content-Range`），供 `<video>` 拖动定位；无 Range 时 200 全量。
### 8.10 `GET /api/jobs/{job_id}/thumbnail`：JPEG，404 若不存在。
### 8.11 `GET /api/jobs/{job_id}/frames/{filename}`：仅允许 `frames/` 下已存在的 jpg，防路径穿越。
### 8.12 `POST /api/jobs/{job_id}/regenerate-summary`
请求 `{}`；同步返回 `{"job": Job}`（在后台线程执行，作业状态先置 running/stage=analyzing，完成后 done；失败 status=error）。若无 Key → 400。
### 8.13 `GET /api/jobs/{job_id}/messages` → `{"messages": [ChatMessage, ...]}`（升序）。
### 8.14 `POST /api/jobs/{job_id}/chat`
请求 `{"question": "...", "stream": true}`。
- `stream=true`：`200`，`Content-Type: text/event-stream`，事件格式（`\n\n` 分隔）：
```
event: delta
data: {"text": "增量文本"}

event: citations
data: {"items": [{"segment_id":3,"start":65.2,"end":71.0,"text":"..."}]}

event: done
data: {"message_id": 12}

event: error
data: {"message": "中文错误"}
```
先 `delta` 流，结束前发 `citations`，最后 `done`。用户消息与助手回复都要落库（`messages` 表）。
- `stream=false`：`200 {"answer":"...","citations":[...],"message":ChatMessage}`。
- 无 Key → 400 `{"detail":"尚未配置 DeepSeek API Key，请在设置中填写。"}`。
### 8.15 `DELETE /api/jobs/{job_id}` → `{"ok": true}`；删除数据库记录与 job_dir；本地来源文件不删。
### 8.16 `GET /api/settings` → `{"settings": SettingsView}`
### 8.17 `PUT /api/settings`（JSON，`SettingsUpdate`，未传字段不变）→ `{"settings": SettingsView}`
### 8.18 `POST /api/settings/test-deepseek` → `{"ok": true, "message": "连接成功（deepseek-chat）", "model": "deepseek-chat"}`；失败 200 也可，`ok=false` + 中文 message。
### 8.19 `POST /api/settings/test-vision` → 可选（若实现，格式同上）。
### 8.20 静态站点：`GET /` 返回 `web/index.html`；`/app.js`、`/style.css` 等静态资源；API 路由优先匹配。CORS：
`allow_origins=["*"]`（本地工具），`allow_methods=["*"]`，`allow_headers=["*"]`；需支持 `chrome-extension://` 来源。
### 8.21 导出（可选但建议）：`GET /api/jobs/{id}/export?format=txt|srt|md` → 文件下载（`Content-Disposition`，文件名 `标题.扩展名`）。

## 9. 作业管理与存储（server-dev）

- `vanalyze/store.py`：SQLite（`data/app.db`，WAL 模式）。表：
  - `jobs(id TEXT PK, status, stage, stage_label, progress REAL, source_type, title, source_url, duration REAL, job_dir TEXT, error TEXT, created_at TEXT, updated_at TEXT, payload TEXT)`
  - `messages(id INTEGER PK AUTOINCREMENT, job_id TEXT, role TEXT, content TEXT, citations TEXT, created_at TEXT)`
- `JobManager`：`ThreadPoolExecutor(max_workers=1)`（Whisper 占显存，串行最稳）；内存中维护每个作业的阶段列表；进度回调更新 DB + 内存（节流：≤5 次/秒）。
- 每个作业目录：`data/jobs/<job_id>/`。上传文件存 `data/uploads/`（server 负责命名与引用；pipeline 收到 `file_path`）。
- 启动时：把 DB 中 `running/pending` 的作业标记为 `error`（"服务重启导致中断，请重新分析"），避免卡死。
- 删除作业：先删 DB，再 `shutil.rmtree(job_dir, ignore_errors=True)`。
- `vanalyze/__main__.py`：`python -m vanalyze`（默认 `serve`：uvicorn 启动；`--host/--port`）。
- 收到作业前先 `config.ensure_dirs()`。

## 10. 前端（ui-dev）

- 调 API 用同源相对路径（`fetch('/api/...')`）；SSE 用 `fetch` + `ReadableStream` 解析（不要用 EventSource，因为要 POST）。
- 轮询：作业 `pending/running` 时每 1.2s `GET /api/jobs/{id}`；`done/error` 停止。
- 上传用 `XMLHttpRequest` 显示上传百分比，然后轮询。
- 页面结构（桌面优先、移动端可用）：
  - 顶栏：产品名「视频分析 · DeepSeek」、后端状态点、设置按钮。
  - 左栏（约 300px）：新建区（拖拽/选择文件、URL 输入+「分析」、扩展提示）、历史作业列表（状态徽章/进度条/时长/删除）。
  - 右主区：空状态引导；作业详情 = 顶部播放器 + 标签页「总结 / 转写 / 画面文字 / 对话」。
  - 播放器 `<video controls>` src=media_url；所有时间戳（章节/转写/引用/画面文字）点击 `currentTime=` 跳转并高亮。
  - 总结页：一句话结论、概览 markdown、章节卡片、要点、关键词、金句、画面线索；无 Key 时显示醒目提示 + 「去设置」。
  - 转写页：搜索框、时间戳列表（等宽时间 + 文本）、复制全文、导出 txt/srt。
  - 画面文字页：时间线 + 缩略图（点击放大）。
  - 对话页：消息气泡（助手 markdown）、流式打字、引用 chips（点击跳转时间）、发送中禁用；输入框 Enter 发送 / Shift+Enter 换行；空态给 3-5 个建议问题（基于 topics）。
  - 设置弹窗：DeepSeek Key（password + 显示切换）、Base URL、模型（deepseek-chat / deepseek-reasoner / 自定义输入）、Whisper 模型/设备、视觉（可选折叠）、保存 + 测试连接；保存后刷新设置。
  - 错误/成功 toast；作业 error 时显示错误消息 + 重试按钮（重新创建作业）。
- 设计：深色专业风（背景 `#0b0e14` 系、卡片 `#141820`、主色 DeepSeek 蓝 `#4d6bfe`、渐变点缀）、圆角 12-16px、`system-ui, "Microsoft YaHei"` 字体、留白充足、微动效；不引外部 CDN（离线可用）。所有界面文案简体中文。
- 文件：`web/index.html`、`web/style.css`、`web/app.js`、`web/markdown.js`（自己写的迷你 markdown 渲染），不要 npm。
- 必须处理：长转写（>1000 段）仍流畅（可分批渲染/懒加载）、视频 URL 未完成时禁用、空数据状态。

## 11. Chrome 扩展（ext-dev）

- MV3，`extension/manifest.json`：`permissions: ["activeTab","scripting","storage","tabs"]`；`host_permissions: ["http://127.0.0.1:8765/*"]`；popup 为 `popup.html`。
- popup 打开时：
  1. 显示当前标签页标题/URL；
  2. 注入 content script 检测 `<video>`（取面积最大/正在播放的），拿 `currentSrc / duration / poster`；
  3. 显示「检测到视频」或「未检测到」；按钮「分析这个视频」→ `POST /api/jobs`（见 §8.3 tab 结构）+ `{source_type:"tab", page_url, video_url, title, duration}`；成功显示作业标题 + 「打开分析界面」按钮（`http://127.0.0.1:8765`）；
  4. 若后端未启动：显示启动提示 + 「重试」按钮；
  5. 后端地址存 `chrome.storage`（默认 `http://127.0.0.1:8765`，popup 里可改，便于端口冲突时调整）。
- 已知站点（bilibili.com、youtube.com、youtu.be、douyin.com、vimeo.com 等）直接传 `page_url`；`video_url` 是 `blob:` 时后端会退化用页面链接，无需前端特殊处理。`currentSrc` 为 http(s) 时一并传。
- 需要图标 `icons/icon16/32/48/128.png`（可用 Pillow 生成，脚本放 `extension/make_icons.py`）。
- `extension/README.md`：安装步骤（chrome://extensions → 开发者模式 → 加载已解压的扩展程序）。
- 不要用 service worker（除非必要）；不申请 `<all_urls>` 权限（`activeTab` + `scripting` 足够）。

## 12. 验收标准（verifier 执行）

1. `python -m pytest tests -q` 全绿（单元 + API 集成，DeepSeek 用本地 stub，不依赖真 Key）。
2. 真实 ASR：用 Windows TTS 生成中文语音 MP4 → 上传 → 转写文本包含预期关键词（≥70% 字符匹配）。
3. 真实 OCR：视频画面画有文字（如「第一章 测试画面」）→ ocr.json 命中。
4. 真实 DeepSeek（若环境有 Key，或用用户 Key 已配置）：总结 JSON 字段完整、章节时间戳合法；问答流式返回且引用时间戳可解析。无 Key 时用 stub 验证链路。
5. Chrome 无头截图：`http://127.0.0.1:8765` 首页与作业详情页渲染正常、无布局错位；`--dump-dom` 含关键元素。
6. Range 请求返回 206；`<video>` 可 seek。
7. 扩展：manifest 可解析、权限最小化、tab 请求体结构正确（静态校验 + 本地 mock 后端验证 POST）。
8. 交付 `docs/VERIFICATION.md`：环境、命令、结果、截图路径、遗留问题。

## 13. 环境与常用命令

### 13.0 已实测的环境事实（Lead 维护，勿改）

- **av 锁定 16.1.0**：faster-whisper 1.2.1 使用 `av.open(metadata_errors=...)`，av>=17 移除该参数会 TypeError。不要升级 av。
- **CUDA 可用**：venv 已装 `nvidia-cudnn-cu12 9.27` / `nvidia-cublas-cu12 12.9` / `nvidia-cuda-runtime-cu12 12.9` / `nvidia-cuda-nvrtc-cu12 12.9`。在 import faster_whisper 前：
  ```python
  import os, sys
  site = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
  for sub in ("cudnn/bin", "cublas/bin", "cuda_runtime/bin", "cuda_nvrtc/bin"):
      p = os.path.join(site, *sub.split("/"))
      if os.path.isdir(p):
          os.add_dll_directory(p)
          os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
  ```
  实测 RTX 4080 SUPER：tiny+cuda/float16 转写 12s 素材 0.9s；large-v3 首次需从 HF 下载约 3GB。
- **中文路径坑**：项目根目录含中文。OpenCV `cv2.imread/imwrite/VideoCapture` 对非 ASCII 路径不可靠：
  图片用 `np.fromfile` + `cv2.imdecode` / `cv2.imencode(...).tofile()`；视频解码优先 PyAV（16.1.0 正常）；ffmpeg 子进程（imageio-ffmpeg）读写中文路径实测正常。
- **测试素材**：`tests/fixtures/sample.mp4`（11.95s，1280x720，中文 TTS 音轨 + 3 张含中文幻灯片），只读。
- **TTS**：`Microsoft Huihui Desktop`（zh-CN）可用。

```powershell
$env:PYTHONUTF8=1
# 启动后端（开发）
.\.venv\Scripts\python.exe -m vanalyze --port 8765
# 跑测试
.\.venv\Scripts\python.exe -m pytest tests -q
# 查看模型设备
.\.venv\Scripts\python.exe -c "from vanalyze.settings import load_settings; from vanalyze.core.asr import detect_device; print(detect_device(load_settings()))"
```
DeepSeek Key 来源优先级：`data/settings.json`（网页设置） < 环境变量 `DEEPSEEK_API_KEY`（环境变量优先）。
GPU 依赖：faster-whisper + ctranslate2；如 CUDA 库缺失，Lead 负责安装 `nvidia-cudnn-cu12`/`nvidia-cublas-cu12` 并保证 `vanalyze/core/gpu.py` 可用。

## 14. 沟通规则

- 任务变更/契约疑问：`send_message` 给 Lead（target `lead`），说明文件与行号；紧急阻塞直接说明。
- 每完成一个里程碑（模块可 import、API 可跑、页面可打开）给 Lead 发一条简短消息。
- 不要 `git init`、不要起长期后台服务占用 8765（测试用 8766/8799 等端口，测完关闭）。
