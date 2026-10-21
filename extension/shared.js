/*
 * 视频分析 · DeepSeek —— Chrome 扩展共享逻辑
 *
 * 本文件同时被 popup.html 与 dev/ 下的本地测试页加载，只依赖标准 Web API：
 *   - 纯函数：地址规范化 / 请求体构造 / 展示格式化 / 站点判断；
 *   - 后端调用：GET /api/health、POST /api/jobs（契约 docs/CONTRACT.md §8.1 §8.3）；
 *   - detectVideosInPage：页面视频检测函数，由 popup 通过
 *     chrome.scripting.executeScript 注入到当前标签页执行。
 *
 * 重要：detectVideosInPage 会被序列化后在页面中执行，
 *       函数体内不能引用本文件中的其他变量或函数。
 */

'use strict';

/** 本地分析服务默认地址（契约 §8 / §11） */
const VA_DEFAULT_BACKEND = 'http://127.0.0.1:8765';

/** chrome.storage.local 中保存后端地址的键 */
const VA_BACKEND_STORAGE_KEY = 'backendUrl';

/** 已知视频站点（契约 §11：优先按 page_url 交给后端解析） */
const VA_KNOWN_VIDEO_HOSTS = [
  'bilibili.com',
  'youtube.com',
  'youtu.be',
  'douyin.com',
  'vimeo.com',
  'ixigua.com',
  'youku.com',
  'iqiyi.com',
  'mgtv.com',
  'tiktok.com',
  'dailymotion.com',
  'twitch.tv',
  'xiaohongshu.com',
];

/** 去掉末尾斜杠（保留 origin，不处理路径语义） */
function trimTrailingSlash(url) {
  return String(url == null ? '' : url).trim().replace(/\/+$/, '');
}

/**
 * 规范化用户在 popup 中输入的后端地址：
 * 补全 http://、去掉末尾斜杠，只保留 origin。
 * @param {string} raw
 * @returns {string} 例如 http://127.0.0.1:8765
 * @throws {Error} 地址不合法时抛出中文错误
 */
function normalizeBackendUrl(raw) {
  let value = String(raw == null ? '' : raw).trim();
  if (!value) throw new Error('本地服务地址不能为空');
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) value = 'http://' + value;
  let parsed;
  try {
    parsed = new URL(value);
  } catch (err) {
    throw new Error('本地服务地址格式不正确');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('只支持 http:// 或 https:// 地址');
  }
  if (!parsed.hostname) throw new Error('本地服务地址格式不正确');
  return parsed.origin;
}

/** 拼接后端 API 地址 */
function backendApiUrl(baseUrl, path) {
  return trimTrailingSlash(baseUrl) + path;
}

/** 分析界面地址（后端根地址，契约 §11） */
function analysisUrl(baseUrl) {
  return trimTrailingSlash(baseUrl) + '/';
}

/** 判断是否已知视频站点 */
function isKnownVideoPage(url) {
  try {
    const hostname = new URL(url).hostname.toLowerCase();
    return VA_KNOWN_VIDEO_HOSTS.some(function (domain) {
      return hostname === domain || hostname.endsWith('.' + domain);
    });
  } catch (err) {
    return false;
  }
}

/** 站点展示名（用于「已知站点」提示） */
function knownSiteLabel(url) {
  try {
    return new URL(url).hostname.replace(/^www\./i, '');
  } catch (err) {
    return '';
  }
}

/** 秒 → mm:ss / h:mm:ss，未知返回「未知」 */
function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return '未知';
  const total = Math.round(value);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = function (num) {
    return num < 10 ? '0' + num : String(num);
  };
  if (hours > 0) return hours + ':' + pad(minutes) + ':' + pad(secs);
  return minutes + ':' + pad(secs);
}

/** 分辨率展示 */
function formatResolution(width, height) {
  const w = Number(width);
  const h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return '未知';
  return Math.round(w) + ' × ' + Math.round(h);
}

/** 视频数量展示 */
function formatVideoCount(detection) {
  const count = detection && Number.isFinite(Number(detection.count)) ? Number(detection.count) : 0;
  if (count <= 0) return '0 个视频';
  const visible = detection && Number.isFinite(Number(detection.visibleCount))
    ? Number(detection.visibleCount)
    : count;
  if (visible > 0 && visible < count) return count + ' 个视频 · ' + visible + ' 个可见';
  return count + ' 个视频';
}

/** 视频来源展示 */
function videoSourceLabel(detection) {
  if (!detection || !detection.videoUrl) return '未暴露地址';
  if (detection.isBlob) return '内嵌流 (blob:)';
  if (detection.isHls) return 'HLS 流';
  try {
    return new URL(detection.videoUrl).hostname || '网络视频';
  } catch (err) {
    return '网络视频';
  }
}

/** 作业状态中文标签（契约 §4 Job.status） */
function statusLabel(status) {
  const map = {
    pending: '排队中',
    queued: '排队中',
    running: '处理中',
    done: '已完成',
    error: '出错',
  };
  return map[String(status || '').toLowerCase()] || '已创建';
}

/**
 * 构造 POST /api/jobs 的 tab 来源请求体（契约 §8.3）。
 * @param {{pageUrl: string, title?: string}} page 当前标签页
 * @param {object|null} video detectVideosInPage 的检测结果（可为空）
 * @returns {{source_type: string, page_url: string, video_url: string|null,
 *            title: string, duration: number, poster: string|null}}
 */
function buildTabJobPayload(page, video) {
  const pageUrl = page && typeof page.pageUrl === 'string' ? page.pageUrl.trim() : '';
  if (!pageUrl) throw new Error('缺少页面地址，无法提交作业');

  const rawDuration = video ? Number(video.duration) : 0;
  const duration = Number.isFinite(rawDuration) && rawDuration > 0
    ? Math.round(rawDuration * 100) / 100
    : 0;

  let title = page && typeof page.title === 'string' ? page.title.trim() : '';
  if (!title && video && typeof video.title === 'string') title = video.title.trim();
  if (!title) title = pageUrl;

  const videoUrl = video && typeof video.videoUrl === 'string' && video.videoUrl.trim()
    ? video.videoUrl.trim()
    : null;
  const rawPoster = video && typeof video.poster === 'string' ? video.poster.trim() : '';
  // poster 只传远程 http(s) 地址，避免把 data:/blob: 大字符串塞进请求体
  const poster = /^https?:\/\//i.test(rawPoster) ? rawPoster : null;

  return {
    source_type: 'tab',
    page_url: pageUrl,
    video_url: videoUrl,
    title: title,
    duration: duration,
    poster: poster,
  };
}

/** 统一解析后端错误响应：FastAPI 的 {"detail": "..."} / 422 数组 / 其他 */
function apiErrorMessage(data, status) {
  if (data && typeof data === 'object') {
    if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim();
    if (Array.isArray(data.detail) && data.detail.length) {
      const parts = data.detail.map(function (item) {
        if (item && typeof item.msg === 'string') return item.msg;
        return typeof item === 'string' ? item : '';
      }).filter(Boolean);
      if (parts.length) return parts.join('；');
    }
    if (typeof data.message === 'string' && data.message.trim()) return data.message.trim();
    if (typeof data.error === 'string' && data.error.trim()) return data.error.trim();
  }
  return '请求失败（HTTP ' + status + '）';
}

/** fetch 异常 → 中文提示与错误类型 */
function classifyFetchError(err) {
  const name = err && err.name ? String(err.name) : '';
  if (name === 'AbortError') {
    return { kind: 'timeout', message: '连接本地服务超时，请确认服务已启动后重试。' };
  }
  return { kind: 'network', message: '无法连接本地服务，请确认已运行启动脚本 run.ps1。' };
}

/**
 * GET /api/health。永不抛错，返回统一结构。
 * @returns {Promise<{ok: true, version: string, deepseekConfigured: boolean,
 *                    asr: object|null, port: number|null}
 *                  | {ok: false, kind: string, status?: number, message: string}>}
 */
async function backendHealth(baseUrl, timeoutMs) {
  const timeout = Number(timeoutMs) > 0 ? Number(timeoutMs) : 2500;
  const controller = new AbortController();
  const timer = setTimeout(function () { controller.abort(); }, timeout);
  try {
    const response = await fetch(backendApiUrl(baseUrl, '/api/health'), {
      method: 'GET',
      cache: 'no-store',
      signal: controller.signal,
    });
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      return {
        ok: false,
        kind: 'http',
        status: response.status,
        message: apiErrorMessage(data, response.status),
      };
    }
    if (!data || data.ok !== true) {
      return { ok: false, kind: 'bad_response', message: '本地服务返回了无法识别的响应。' };
    }
    return {
      ok: true,
      version: typeof data.version === 'string' ? data.version : '',
      deepseekConfigured: data.deepseek_configured === true,
      asr: data.asr && typeof data.asr === 'object' ? data.asr : null,
      port: Number.isFinite(Number(data.port)) ? Number(data.port) : null,
    };
  } catch (err) {
    return Object.assign({ ok: false }, classifyFetchError(err));
  } finally {
    clearTimeout(timer);
  }
}

/**
 * POST /api/jobs（tab 来源）。永不抛错，返回统一结构。
 * @returns {Promise<{ok: true, job: object} | {ok: false, kind: string, status?: number, message: string}>}
 */
async function submitTabJob(baseUrl, payload, timeoutMs) {
  const timeout = Number(timeoutMs) > 0 ? Number(timeoutMs) : 20000;
  const controller = new AbortController();
  const timer = setTimeout(function () { controller.abort(); }, timeout);
  try {
    const response = await fetch(backendApiUrl(baseUrl, '/api/jobs'), {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      return {
        ok: false,
        kind: 'http',
        status: response.status,
        message: apiErrorMessage(data, response.status),
      };
    }
    const job = data && typeof data === 'object' ? data.job : null;
    if (!job || typeof job !== 'object' || !job.id) {
      return { ok: false, kind: 'bad_response', message: '本地服务返回的作业信息不完整。' };
    }
    return { ok: true, job: job };
  } catch (err) {
    return Object.assign({ ok: false }, classifyFetchError(err));
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ */
/* 页面视频检测（注入到当前标签页执行；函数体必须自包含）              */
/* ------------------------------------------------------------------ */

/**
 * 统计页面中的 <video>，选出「可见 + 正在播放 + 面积最大」的一个。
 * 支持主文档、同源 iframe（递归）与 Shadow DOM；跨域 iframe 会跳过而不报错。
 * 由 popup 通过 chrome.scripting.executeScript({func}) 注入，因此
 * 函数体内不能引用外部变量（本函数以下划线开头的内部函数均为自包含的）。
 *
 * @returns {{ok: boolean, found: boolean, count: number, visibleCount: number,
 *            videoUrl: string|null, duration: number, poster: string|null,
 *            pageUrl: string, title: string, playing: boolean, width: number,
 *            height: number, isBlob: boolean, isHls: boolean,
 *            frameCount: number, blockedFrameCount: number}}
 */
function detectVideosInPage() {
  const MAX_DEPTH = 4;
  const MAX_SHADOW_ELEMENTS = 20000;
  const videos = [];
  const seenVideos = new Set();
  let framesScanned = 0;
  let framesBlocked = 0;

  function addVideosFrom(root) {
    let list;
    try {
      list = root.querySelectorAll('video');
    } catch (err) {
      return;
    }
    for (let i = 0; i < list.length; i += 1) {
      const video = list[i];
      if (!seenVideos.has(video)) {
        seenVideos.add(video);
        videos.push(video);
      }
    }
  }

  function scanShadowRoots(root, depth) {
    if (depth > MAX_DEPTH) return;
    let elements;
    try {
      elements = root.querySelectorAll('*');
    } catch (err) {
      return;
    }
    const limit = Math.min(elements.length, MAX_SHADOW_ELEMENTS);
    for (let i = 0; i < limit; i += 1) {
      let shadow = null;
      try {
        shadow = elements[i].shadowRoot;
      } catch (err) {
        shadow = null;
      }
      if (shadow) scanRoot(shadow, depth + 1);
    }
  }

  function scanFrames(root, depth) {
    if (depth >= MAX_DEPTH) return;
    let frames;
    try {
      frames = root.querySelectorAll('iframe, frame');
    } catch (err) {
      return;
    }
    for (let i = 0; i < frames.length; i += 1) {
      let doc = null;
      try {
        doc = frames[i].contentDocument;
      } catch (err) {
        doc = null;
      }
      if (doc) {
        framesScanned += 1;
        scanRoot(doc, depth + 1);
      } else {
        framesBlocked += 1;
      }
    }
  }

  function scanRoot(root, depth) {
    if (!root || depth > MAX_DEPTH) return;
    addVideosFrom(root);
    scanFrames(root, depth);
    scanShadowRoots(root, depth);
  }

  function videoUrlOf(video) {
    try {
      const current = video.currentSrc || video.src || '';
      return String(current).trim();
    } catch (err) {
      return '';
    }
  }

  function isVisible(video) {
    try {
      const view = video.ownerDocument && video.ownerDocument.defaultView;
      const style = view && view.getComputedStyle ? view.getComputedStyle(video) : null;
      if (!style) return false;
      if (style.display === 'none') return false;
      if (style.visibility === 'hidden' || style.visibility === 'collapse') return false;
      if (parseFloat(style.opacity) === 0) return false;
      const rect = video.getBoundingClientRect();
      return rect.width >= 2 && rect.height >= 2;
    } catch (err) {
      return false;
    }
  }

  function visibleArea(video) {
    try {
      const rect = video.getBoundingClientRect();
      return Math.max(0, rect.width) * Math.max(0, rect.height);
    } catch (err) {
      return 0;
    }
  }

  function isPlaying(video) {
    try {
      return !video.paused && !video.ended && video.readyState >= 2;
    } catch (err) {
      return false;
    }
  }

  scanRoot(document, 0);

  const scored = [];
  let visibleCount = 0;
  for (let i = 0; i < videos.length; i += 1) {
    const video = videos[i];
    const visible = isVisible(video);
    if (visible) visibleCount += 1;
    const playing = isPlaying(video);
    const hasUrl = videoUrlOf(video) ? 1 : 0;
    // 优先级：可见播放中 > 可见 > 后台播放 > 其他；同档位比可见面积。
    const tier = (visible ? 4 : 0) + (playing ? 2 : 0) + hasUrl;
    const score = tier * 1e10 + Math.min(visibleArea(video), 5e7);
    scored.push({ video: video, score: score, playing: playing });
  }

  let best = null;
  for (let i = 0; i < scored.length; i += 1) {
    if (!best || scored[i].score > best.score) best = scored[i];
  }

  const bestVideo = best ? best.video : null;
  const bestUrl = bestVideo ? videoUrlOf(bestVideo) : '';
  const rawDuration = bestVideo ? Number(bestVideo.duration) : 0;
  const duration = Number.isFinite(rawDuration) && rawDuration > 0
    ? Math.round(rawDuration * 100) / 100
    : 0;
  let poster = null;
  try {
    poster = bestVideo && typeof bestVideo.poster === 'string' && bestVideo.poster
      ? bestVideo.poster
      : null;
  } catch (err) {
    poster = null;
  }

  return {
    ok: true,
    found: !!bestVideo,
    count: videos.length,
    visibleCount: visibleCount,
    videoUrl: bestUrl || null,
    duration: duration,
    poster: poster,
    pageUrl: location.href,
    title: (document.title || '').trim(),
    playing: best ? best.playing : false,
    width: bestVideo ? Number(bestVideo.videoWidth) || 0 : 0,
    height: bestVideo ? Number(bestVideo.videoHeight) || 0 : 0,
    isBlob: /^blob:/i.test(bestUrl),
    isHls: /\.m3u8(\?|#|$)/i.test(bestUrl),
    frameCount: framesScanned,
    blockedFrameCount: framesBlocked,
  };
}
