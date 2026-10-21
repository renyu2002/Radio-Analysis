# 视频分析 · DeepSeek —— 独立验收报告

> 验收者：verifier（独立于实现者）  
> 验收对象：`<PROJECT_ROOT>`（下称 ROOT）  
> 依据：`docs/CONTRACT.md`（§4–§13）+ 团队任务 task-5 验收要求  
> 报告生成：2026-10-21；**最终回归：2026-10-21 16:43–16:46（集成完成后全量重跑，见 §3.0）**

---

## 1. 环境事实（实测）

| 项目 | 实测值 |
|---|---|
| 操作系统 | Windows 10（10.0.19045），本地工具，离线可运行 |
| Python | 3.12.10，venv：`ROOT\.venv\Scripts\python.exe` |
| Web 框架 | FastAPI 0.156.6 + uvicorn 0.54.0 + pydantic 2.14.1 + httpx 0.28.1 |
| ASR | faster-whisper 1.2.1，ctranslate2 4.8.3，av 16.1.0（锁定版） |
| CUDA | `gpu.status()`：ok=True，设备数 1，cuDNN/cuBLAS 可加载 → ASR 走 `cuda/float16` |
| OCR | rapidocr + onnxruntime 1.31.0（模型进程内缓存） |
| 视频处理 | opencv 5.0.0、imageio-ffmpeg 0.6.0、yt-dlp（网页来源） |
| 模型缓存 | HF 缓存已有 `faster-whisper-large-v3`（约 2.95 GB）与 `faster-whisper-tiny` |
| Chrome | 155.0.8059.85（`C:\Program Files\Google\Chrome\Application\chrome.exe`） |
| pytest | 9.1.1（由 Lead 安装；`python -m pytest` 可用） |
| DeepSeek Key | 验收期间用户 `data/settings.json` 已配置真实 Key，真实 API 验收通过（Key 仅掩码记录，见 §3.5） |
| 测试隔离 | 全部测试通过 `VANALYZE_DATA_DIR` 指向系统临时目录，**不写用户 `data/`**；stub 服务随机端口，真实服务用临时端口（8798/8799/8803 等，绝不占用 8765） |

---

## 2. 测试资产（`tests/**`，verifier 交付）

| 文件 | 作用 |
|---|---|
| `tests/stub_deepseek.py` | 本地 OpenAI 兼容 DeepSeek stub（端口默认 8777）：普通中文回答 / `response_format=json_object` 的完整 Summary JSON / SSE 分片 + `[DONE]` / Bearer 任意 key / 请求记录 / 故障注入标记（`[STUB:FENCED]`、`[STUB:TRUNCATED]`、`[STUB:SIMPLE_JSON]`、`[STUB:NOT_JSON]`、`[STUB:STREAM_401/500]`） |
| `tests/conftest.py` | pytest 夹具：临时数据目录隔离、清理真实 Key 环境变量、本地 stub、注入 `JobManager` 的 TestClient、Settings 工厂；注册 e2e/slow/real_key/extension marker |
| `tests/helpers.py` | 种子作业（DB + `data/jobs/<id>/` 产物）、SSE 解析、中文相似度（≥70% 字符匹配）、轮询、Chrome 定位等 |
| `tests/test_units.py` | 46 个单元测试：models / settings（掩码、环境变量优先级、部分更新）/ store / server 纯函数（MM:SS、SRT、上传名、Range 辅助）/ core 纯函数（bigram 分词+BM25、`_parse_json_text` JSON 修复、章节兜底 `_coerce_chapters`、`_fmt_ts`、reasoner payload、ingest URL 判定、`probe_media`、产物 JSON UTF-8） |
| `tests/test_api.py` | 28 个 API 集成测试：§8 全部关键端点（health/settings/upload/url/tab/jobs/transcript/summary/ocr/chat SSE+非流式/regenerate/messages/export/media Range 206/416/thumbnail/frames 路径穿越/400/404/DELETE/静态站点/CORS/test-deepseek）；DeepSeek 用本地 stub |
| `tests/e2e.py` | 真实服务（8798）+ 真实 pipeline 端到端：上传 `sample.mp4` → 轮询 → done；真实 ASR 关键词与 ≥70% 字符匹配；真实 OCR；summary 字段/章节；SSE 问答；Range；可选 URL 来源（`E2E_URL=1`，slow） |
| `tests/ui_smoke.py` | 真实服务（8799/8803）+ 预置完整作业；Chrome headless 截图首页/详情页（1440×900）+ `--dump-dom` 25 项关键元素检查；CDP 真实点击标签页/设置弹窗并截图 5 张；产物 `tests/.artifacts/` |
| `tests/test_extension.py` | 12 个扩展验收：manifest/权限/图标/CSP 静态检查、mock 后端契约、Chrome headless 真实执行共享逻辑（检测/请求体/fetch）+ popup 四状态 DOM 断言 + popup 截图 |
| `tests/real_key_check.py` | 真实 DeepSeek Key 验收（独立脚本）：连通性、真实总结、真实问答（含流式），读取用户配置但绝不输出 Key；无 Key 时安全跳过 |

> `tests/fixtures/sample.mp4`、`make_fixture.py`、`slide*.png`、`speech.wav` 为 Lead 提供的只读素材，未修改。  
> 测试全程**未修改** `vanalyze/**`、`web/**`、`extension/**`、`docs/CONTRACT.md`、`requirements.txt`、`run.ps1`、`setup.ps1`。

---

## 3. 执行结果

### 3.0 最终回归（集成完成后）

> 触发：Lead 通知「集成完成，开始最终验收」（task-1 core 含 OCR 优化/VAD 兜底、task-2 server、task-3 web、task-4 extension 均 completed）。  
> 环境：集成后代码（`vanalyze/core/vision.py` 16:36、`asr.py` 16:30、`web/app.js` 16:34 等）；**隔离数据目录 + 临时端口**，未触碰 8765 的 data/ 与设置。  
> 失败项：**无**。以下 5 条命令全部通过（原始日志：`tests/.artifacts/final/*.log`）。

| # | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `.venv\Scripts\python.exe -m pytest tests -q` | **86 passed，0 failed，0 skipped** | 16.2 s |
| 2 | `E2E_WHISPER_MODEL=large-v3; .venv\Scripts\python.exe tests\e2e.py` | **7 passed，1 skipped**（URL 用例默认 skip）；ASR 字符匹配 **100.0%**，作业 11.0 s | 13.4 s |
| 3 | `.venv\Scripts\python.exe tests\real_key_check.py` | **10 passed，0 failed**（真实 DeepSeek API，Key 掩码） | 9.3 s |
| 4 | `UI_SMOKE_PORT=8803; .venv\Scripts\python.exe tests\ui_smoke.py` | **25/25 项通过** + CDP 交互截图 5 张 | 9.9 s |
| 5 | `.venv\Scripts\python.exe -m pytest tests\test_extension.py -q` | **12 passed** | 5.1 s |

> 说明：原 `test_api.py` / `test_units.py` / `test_extension.py` 共 86 项已包含在第 1 条中；扩展单跑（第 5 条）用于单独取证。URL 来源 E2E 见 §3.6（集成前验证，通过）。  
> 最终回归后再次确认：无残留测试服务（8798/8799/8803 等端口已释放），仅保留 Lead 的 8765 单实例；临时数据目录已清理。


### 3.1 单元 + API 测试（验收标准 §12.1）

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe -m pytest tests -q
```

结果：**86 passed，0 failed，0 skipped**（约 16.6 s）。分布：`test_units.py` 46、`test_api.py` 28、`test_extension.py` 12。

> 覆盖方式：API 测试使用 FastAPI `TestClient` + 本地 stub DeepSeek；`JobManager._run_job` 被替换为快速失败桩，避免 API 层测试触发真实 ASR/网络。真实管线由 E2E 验证。

### 3.2 真实 ASR + OCR + 总结（验收标准 §12.2 / §12.3 / §12.4）

命令（real 服务真实 pipeline；ASR 模型可按环境变量切换）：

```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe tests\e2e.py                          # tiny 模型
$env:E2E_WHISPER_MODEL="large-v3"; .\.venv\Scripts\python.exe tests\e2e.py
```

| 模型 | 结果 | 作业耗时 | ASR 字符匹配率 | 转写摘要 |
|---|---|---|---|---|
| tiny | 7 passed, 1 skipped（URL 用例默认 skip） | 13.8 s | 95.2% | 今天天气很好,我们来测试语音转文字功能。这是视频分析系统的字简音频,请确认转写结果是否准确。 |
| large-v3 | 7 passed, 1 skipped | 12.8 s | **100.0%** | 今天天气很好,我们来测试语音转文字功能。这是视频分析系统的自检音频,请确认转写结果是否准确。 |
| **large-v3（最终回归，OCR 优化后）** | **7 passed, 1 skipped**（总 13.4 s） | **11.0 s** | **100.0%** | 同上（自检音频，标点正确） |

- ASR 关键词命中：`语音转文字`、`天气`；device=`cuda`，compute_type=`float16`。
- OCR 命中（RapidOCR 3.10）：`第一章 测试画面`、`画面文字 OCR 测试`、`语音转文字测试`，带 `[MM:SS]` 时间线（最终回归 3/3 命中）。
- Summary 字段完整（title/one_liner/overview/topics/chapters/key_points/keywords/quotes/visual_notes/generated_by/created_at），章节时间戳均满足 `0 ≤ start < end ≤ 11.95`。
- 媒体 Range：`bytes=1000-2999` → 206 + `Content-Range: bytes 1000-2999/<size>`，供 `<video>` seek。
- 问答 SSE：`delta → citations → done`，引用时间戳合法，消息落库。
- 产物留档：`tests/.artifacts/e2e/{job,transcript,ocr,summary}.json`；服务日志 `tests/.artifacts/e2e_server.log` / `final/e2e_large_v3.log`。

**服务端真实联调冒烟（独立执行 `vanalyze.server._smoke`，README 中记录的命令）**：

```powershell
$env:VANALYZE_DATA_DIR = "<临时目录>"
.\.venv\Scripts\python.exe -m vanalyze.server._smoke
```

结果：**21/21 通过，总耗时 40.0 s**（临时 8799 服务 + 临时数据目录，不污染用户数据）。large-v3/cuda 转写 2 段 47 字；OCR 3 帧 117 字并带时间线；media `Range: bytes=0-1023` → 206 `Content-Range: bytes 0-1023/1102848`；导出 txt/srt/md 正常；无 Key 时 chat 返回 400 中文降级；作业 done 后清理。

### 3.2.1 core 性能优化实测（集成汇报 + 代码核对）

> 来源：core-dev 交付汇报（Lead 汇总）；verifier 已核对代码中对应实现，并在最终 E2E 中确认无回归（fixture OCR 3/3 命中、large-v3 ASR 100%、总耗时 13.4 s）。

**OCR 性能优化（`vanalyze/core/vision.py`）**：

| 优化项 | 代码核对 | 效果（core-dev A/B 实测） |
|---|---|---|
| 关键帧硬上限 100 → 60 | `MAX_FRAMES_HARD_LIMIT = 60`（vision.py:29） | 减少 OCR 帧数 |
| OCR 前降采样宽度 960 | `_OCR_MAX_WIDTH = 960`（vision.py:27） | 单帧耗时下降 |
| 关闭方向分类器 | `engine(target, use_cls=False)`（vision.py:333，旧版 API 自动回退） | 单帧耗时下降 |
| 合计 | — | **A/B 约 2.2–2.4× 加速**；fixture OCR 仍 3/3 命中 |

**长视频总结 map-reduce（`vanalyze/core/analyze.py`）**：

- 代码核对：`_CHUNK_TARGET_CHARS = 9000`、`_CHUNK_MAX = 20`，先 map 分段摘要再 reduce 生成结构化 Summary。
- core-dev 实测：79.6k 字转写 → 22 次 API → 82 s，输出 JSON/章节时间戳合法。

### 3.3 真实 DeepSeek Key（验收标准 §12.4，条件可用）

```powershell
.\.venv\Scripts\python.exe tests\real_key_check.py
```

结果：**10 passed / 0 failed**（真实 `deepseek-chat`，Key 掩码 `sk-***`，仅本地临时 job_dir，不写用户数据；最终回归脚本总耗时 9.3 s）。

- `test_connection` 连通；真实总结耗时约 3.9 s，`generated_by="deepseek-chat"`，3 个章节时间戳合法，overview 370 字。
- 真实问答：回答带 `【MM:SS】` 引用（如「8 秒处画面显示『语音转文字测试…』【00:08】」），citations 来自本地 BM25 检索且时间戳合法；真实流式问答逐段返回。
- 产物留档：`tests/.artifacts/real_key.json`（不含 Key）；原始日志 `tests/.artifacts/final/real_key.log`。  
- 无 Key 环境下同一链路由 `tests/stub_deepseek.py` 覆盖（API 测试 + E2E 默认 stub）。

### 3.4 UI 冒烟 + Chrome 截图（验收标准 §12.5）

```powershell
$env:UI_SMOKE_PORT="8803"; .\.venv\Scripts\python.exe tests\ui_smoke.py
```

结果：**25/25 项通过**（最终回归 9.9 s，含 CDP 交互截图）。真实服务 + 预置完整作业；Chrome headless（`--headless=new --disable-gpu --virtual-time-budget --force-prefers-reduced-motion`）。

| 截图 | 路径 | 说明 |
|---|---|---|
| 首页 | `tests/.artifacts/home.png`（1440×900） | 顶栏产品名/服务状态/设置、新建分析（拖拽区+URL+扩展提示）、历史作业（已完成徽章/时长/段数）、空状态引导、四功能卡片；无布局错位 |
| 作业详情（总结） | `tests/.artifacts/detail.png`、`detail_summary.png`（1440×900） | 作业标题/已完成徽章/来源/时长/段数、播放器卡片、四标签页、一句话结论+概览+要点+章节；无错误横幅 |
| 转写标签 | `tests/.artifacts/detail_transcript.png`（1440×900） | 搜索框、3 段带 `MM:SS` 时间戳转写、复制全文/导出按钮、共 3 段统计 |
| 画面文字标签 | `tests/.artifacts/detail_ocr.png`（1440×900） | 3 帧时间线（00:00/00:04/…）、置信度、缩略图、OCR 文本（中文渲染正常） |
| 对话标签 | `tests/.artifacts/detail_chat.png`（1440×900） | 空态引导、基于 topics 的 4 个建议问题、输入框与发送按钮、`【MM:SS】` 提示 |
| 设置弹窗 | `tests/.artifacts/settings_modal.png`（1440×900） | Key 掩码「已配置（sk-u…3456），留空则不修改」、Base URL、模型下拉、Whisper 模型/设备、后端当前设备 CUDA(float16)、视觉模型折叠、测试连接/保存 |

- 交互截图方式：扩展 `tests/ui_smoke.py`，用 Chrome DevTools Protocol（websockets）真实点击标签页/设置按钮后 `Page.captureScreenshot`（`tests/.artifacts/ui_smoke.json` 记录 `interaction_screenshots`）。
- DOM 检查项：产品名、DeepSeek 品牌、新建分析、历史作业、**历史作业列表项（真实作业标题）**、设置按钮、拖拽区 `#dropzone`、设置弹窗 `#settingsModal`、空状态、作业标题、`#tab-summary/#tab-transcript/#tab-ocr/#tab-chat`、`#video`、播放器媒体地址、总结内容渲染、错误横幅隐藏、转写数量=3、完成徽章——全部 PASS。
- 结果文件：`tests/.artifacts/ui_smoke.json`；DOM 快照：`tests/.artifacts/home.html`、`detail.html`；原始日志 `tests/.artifacts/final/ui_smoke.log`。
- **播放器编码兜底验证（新增功能，实测通过）**：`tests/fixtures/sample.mp4` 的视频轨是 **MPEG-4 Part 2（mp4v）**，Chrome 无法解码，详情页正确显示「浏览器无法播放该视频编码（如 HEVC / MPEG-4），但并不影响转写、总结与问答」+「下载原视频」兜底卡片（未出现崩溃/白屏）；真实浏览器中 H.264 视频播放正常（见下方 `docs/images/chat.png` 真实截图，播放器显示视频画面与原生控件）。
- 另：仓库 `docs/images/` 内附 Lead 从真实 8765 作业截取的 `home.png`、`detail.png`、`chat.png`（README 引用），可作为真实浏览器渲染的补充参考。其中 `chat.png` 实测可见：播放器**真实视频画面**（「第一章 测试画面」幻灯片）正常渲染、对话页真实 DeepSeek 回答带 `【MM:SS】` 引用 chips、转写/画面文字计数、历史列表含进行中 URL 作业与进度条。
- **截图说明（已实测验证）**：Chrome headless 无法把 `<video>` 解码画面合成进截图（用纯本地 `<video>` 测试页同样全黑），因此 headless 截图中播放器区域不可见属浏览器限制，不代表页面问题；视频播放由 `docs/images/chat.png`（真实浏览器）+ DOM 结构 + 媒体端点 + HTTP Range 206 测试共同保证。`--force-prefers-reduced-motion` 用于让列表淡入动画停在最终态，避免截图偶发发灰。

### 3.5 Chrome 扩展（验收标准 §12.7）

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_extension.py -q
```

结果：**12 passed**（最终回归 5.1 s；产物 `tests/.artifacts/extension/`，原始日志 `tests/.artifacts/final/extension.log`）。

- `manifest.json`：MV3；`permissions` 精确为 `activeTab/scripting/storage/tabs`；`host_permissions` 仅 `http://127.0.0.1:8765/*`；无 `<all_urls>`、无 background service worker；图标 16/32/48/128 尺寸正确（PNG）。
- popup：无内联脚本/事件、无外部 http(s) 资源（离线可用）；`shared.js` 先于 `popup.js`；含「分析这个视频」「打开分析界面」「当前页面」「本地服务地址」「重试」。
- 共享逻辑在 Chrome headless 中真实执行：`detectVideosInPage`（播放中/最大/跳过隐藏、同源 iframe 递归、blob 识别）、`buildTabJobPayload` 字段与 §8.3 完全一致（source_type/page_url/video_url/title/duration/poster，duration 数字）、真实 `fetch` 到 mock 后端成功、后端未启动时优雅失败——**零失败**。
- mock 后端收到 1 次 POST `/api/jobs`，请求体字段精确、`Content-Type: application/json`、带 Referer；非法请求 400 且不记录。
- popup 四状态（detected/offline/novideo/success）DOM 断言通过：检测到视频显示时长 `12:34`/分辨率/来源与「已连接」；后端未启动显示「本地服务未启动」+ 禁用按钮；无视频但已知站点降级「按页面链接分析」；提交成功显示成功卡片与「打开分析界面」。
- 截图：`tests/.artifacts/extension/popup_detected.png`（420×720，实测视觉正常）。

### 3.6 URL 来源（验收标准 §12 附属，可选 slow）

```powershell
$env:E2E_URL="1"; .\.venv\Scripts\python.exe tests\e2e.py -k url_source
```

结果：**1 passed**（316.3 s，隔离 8798 服务 + 临时数据目录；B 站真实短视频）。

| 阶段 | 耗时（相对作业创建） | 说明 |
|---|---|---|
| downloading | 1.0 s | yt-dlp 解析并下载 `https://www.bilibili.com/video/BV1GJ411x7h7` |
| transcribing | 5.1 s | 真实 ASR（tiny/cuda）完成 |
| frames | 47.9 s | 212 s 视频抽帧（最多 100 帧） |
| ocr | 83.3 s → 309.7 s | 100 帧 RapidOCR（并发验收负载下约 3.8 分钟，见 §6 遗留项） |
| analyzing → done | 309.7 → 310.7 s | stub DeepSeek 总结，完成 |

- 作业 `done`，标题正确提取：`【官方 MV】Never Gonna Give You Up - Rick Astley`，时长 212.3 s，35 段转写非空；媒体 `Range: bytes=0-99` → 206。
- 失败重试路径：上一版用 34 分钟长视频在 300 s 内未跑完（已到 OCR 阶段），说明 URL 链路对长视频需更长等待；验收脚本已改为 3.5 分钟视频 + 600 s 超时。
- 注：该次运行在 OCR 优化（60 帧硬上限/960 宽/use_cls=False）合入前，抽帧与 OCR 按当时配置 100 帧执行（3.8 分钟）；优化后在同样场景预计明显更快（§3.2.1）。
- 日志：`tests/.artifacts/url_e2e_run.log`。


---

## 4. 验收标准逐项对照（CONTRACT §12）

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | `python -m pytest tests -q` 全绿（单元+API，DeepSeek 用 stub） | ✅ 86 passed / 0 skipped | §3.1 |
| 2 | 真实 ASR：TTS 中文 MP4，转写 ≥70% 字符匹配 | ✅ tiny 95.2%，large-v3 100.0%，关键词命中 | §3.2 |
| 3 | 真实 OCR：画面文字命中 | ✅ `第一章 测试画面`/`画面文字 OCR 测试`/`语音转文字测试` | §3.2 |
| 4 | 真实 DeepSeek（有 Key）：总结字段完整、章节时间戳合法；问答流式+引用可解析 | ✅ 真实 Key 10/10；无 Key 用 stub 覆盖 | §3.2 / §3.3 |
| 5 | Chrome 无头截图首页/详情页正常；`--dump-dom` 含关键元素 | ✅ 25/25 + CDP 点击截取总结/转写/画面文字/对话/设置 5 张；真实浏览器截图 `docs/images/*.png` | §3.4 |
| 6 | Range 请求 206；`<video>` 可 seek | ✅ `bytes=1000-2999` → 206；`Accept-Ranges`/`Content-Range` 正确 | §3.2 / API 测试 |
| 7 | 扩展 manifest/权限最小化/tab 请求体（静态+mock） | ✅ 12/12 | §3.5 |
| 8 | 交付 `docs/VERIFICATION.md` | ✅ 本文件 | — |

---

## 5. 失败项与复现步骤

最终回归结果见 §3.0。截至本报告生成，**没有未解决的失败项**。

验收过程中发现并已规避的测试侧问题（非产品缺陷，记录备查）：

1. **测试进程无法 import `vanalyze`（直接运行 `python tests/e2e.py`）**  
   - 原因：脚本方式运行时 `sys.path[0]` 为 `tests/`，项目根目录不在 `sys.path`。  
   - 处理：`tests/e2e.py`、`tests/ui_smoke.py`、`tests/conftest.py` 显式把 ROOT 加入 `sys.path`。已回归。
2. **SSE 错误事件用例耗时 15 s**  
   - 原因：原用例把 base_url 指向不可达端口，触发 httpx connect 超时。  
   - 处理：改用 stub 故障注入标记 `[STUB:STREAM_401]`，确定性立即返回 401 → SSE `error` 事件，耗时 <1 s。已回归。
3. **UI 截图历史列表发灰**  
   - 原因：Chrome headless 在 `fade-in` 动画起始帧截图。  
   - 处理：加 `--force-prefers-reduced-motion`（页面已支持 prefers-reduced-motion），截图稳定在最终态。已回归。
4. **UI 截图播放器区域全黑 / 编码兜底**  
   - 原因：① Chrome headless 不合成 `<video>` 解码画面（用纯本地 `<video>` 页复现同样现象）；② 验收素材 `sample.mp4` 视频轨为 MPEG-4 Part 2（mp4v），Chrome 本身不支持该编码。  
   - 处理：应用的新「编码兜底」UI 正确显示中文提示 + 下载按钮（实测通过）；真实浏览器中 H.264 视频播放正常（`docs/images/chat.png`）。播放器功能以 DOM/媒体端点/Range 验证，报告如实说明限制。

---

## 6. 遗留问题与建议

1. **UI 交互**：本次已用 CDP 真实点击标签页/设置弹窗并截取 5 张交互截图（§3.4），布局与数据渲染均正常；仍建议交付前在真实浏览器完整点一遍：视频播放/拖动 seek、时间戳跳转、聊天流式打字、设置保存后刷新。
2. **OCR 小字召回**：RapidOCR 对低分辨率、艺术字、过小字号文字召回有限（本次 fixture 3/3 命中）；长视频/网课 PPT 小字场景建议人工抽查或在设置中调大帧数量。
3. **`use_cls=False` 与竖排文字**：为提速关闭了方向分类器，竖排/旋转文字可能漏检；如遇竖排字幕场景可调整该参数（`vanalyze/core/vision.py`）。
4. **YouTube 等站点依赖 JS runtime**：yt-dlp 解析 YouTube 部分格式需要 JS runtime（如 deno/node）支持；未安装时可能只能拿到降级格式或失败。B 站 URL 已真实验证通过（§3.6）。
5. **超长视频未实测**：长转写 map-reduce 实测 79.6k 字 → 22 次 API → 82 s 合法（§3.2.1）；>180k 字级超长视频未做端到端实测，成本和耗时需真实场景评估。
6. **URL 长视频耗时**：集成后 OCR 已优化（60 帧上限 / 960 宽 / use_cls=False），短视频实测 316 s；34 分钟级长视频建议给更长超时（`E2E_URL_TIMEOUT`）或调大 `frame_interval`。
7. **真实 Key 会消耗用户额度**：`real_key_check.py` 每次约 3 次真实 API 调用（连通性+总结+问答）；默认 pytest 不含该脚本，需显式运行。
8. **模型缓存依赖**：E2E 默认 tiny；large-v3 需 HF 缓存/联网下载（本机已缓存）。首次加载 large-v3 约 3–5 s。
9. **Chrome headless 局限**：视频画面不参与截图（见 §5.4）；真实浏览器截图由 `docs/images/*.png` 补充。

---

## 7. 常用复现命令汇总

```powershell
$env:PYTHONUTF8=1
# 1) 全部单元 + API + 扩展测试（无真实 Key 依赖）
.\.venv\Scripts\python.exe -m pytest tests -q

# 2) E2E：真实服务 8798 + 真实 pipeline（tiny 快速；large-v3 真实验收）
.\.venv\Scripts\python.exe tests\e2e.py
$env:E2E_WHISPER_MODEL="large-v3"; .\.venv\Scripts\python.exe tests\e2e.py

# 3) 真实 DeepSeek Key 验收（读取 data/settings.json 或 DEEPSEEK_API_KEY）
.\.venv\Scripts\python.exe tests\real_key_check.py

# 4) UI 冒烟 + 截图（8799；端口占用时改 UI_SMOKE_PORT）
.\.venv\Scripts\python.exe tests\ui_smoke.py

# 5) 可选 URL 来源 E2E（联网）
$env:E2E_URL="1"; .\.venv\Scripts\python.exe tests\e2e.py -k url_source

# 6) 单独启动 stub DeepSeek（调试用，8777）
.\.venv\Scripts\python.exe tests\stub_deepseek.py --port 8777
```

---

## 8. 产物清单

**`tests/.artifacts/`（测试运行产物）**：

```
home.png / detail.png              UI 冒烟截图（1440×900）
detail_summary.png                 作业详情-总结标签（CDP）
detail_transcript.png              作业详情-转写标签（CDP）
detail_ocr.png                     作业详情-画面文字标签（CDP）
detail_chat.png                    作业详情-对话标签（CDP）
settings_modal.png                 设置弹窗（CDP）
home.html / detail.html            对应 DOM 快照
ui_smoke.json                      UI 检查项结果（25 项）+ 交互截图记录
ui_smoke_server.log / e2e_server.log
url_e2e_run.log                    URL 来源 E2E 日志
e2e/job.json transcript.json ocr.json summary.json   真实 E2E 产物快照
real_key.json                      真实 Key 验收结果（不含 Key）
extension/popup_detected.png       popup 截图（420×720）
extension/browser_test.json        Chrome 内共享逻辑断言结果
extension/popup_{detected,offline,novideo,success}.json   popup 状态快照
final/pytest.log                  最终回归：pytest 原始日志
final/e2e_large_v3.log            最终回归：E2E large-v3 原始日志
final/real_key.log                最终回归：真实 Key 原始日志
final/ui_smoke.log                最终回归：UI 冒烟原始日志
final/extension.log               最终回归：扩展测试原始日志
```

**`docs/images/`（Lead 从真实 8765 作业截取，README 引用）**：`home.png`、`detail.png`、`chat.png`（真实浏览器渲染，含视频画面与 DeepSeek 对话）。
