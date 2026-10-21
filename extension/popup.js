/*
 * 视频分析 · DeepSeek —— Chrome 扩展 popup 逻辑
 *
 * 流程（契约 docs/CONTRACT.md §11）：
 *   1. chrome.tabs.query 读取当前活动标签页；
 *   2. chrome.scripting.executeScript 注入 detectVideosInPage 检测 <video>；
 *   3. GET /api/health 检查本地服务，POST /api/jobs 提交 tab 来源作业；
 *   4. 成功后展示作业标题与「打开分析界面」按钮。
 *
 * 依赖同目录 shared.js（需先于本文件加载）。
 */

'use strict';

(function () {
  var el = {
    pageTitle: document.getElementById('page-title'),
    pageUrl: document.getElementById('page-url'),
    detectIcon: document.getElementById('detect-icon'),
    detectTitle: document.getElementById('detect-title'),
    detectSub: document.getElementById('detect-sub'),
    detectBadge: document.getElementById('detect-badge'),
    metaGrid: document.getElementById('meta-grid'),
    metaDuration: document.getElementById('meta-duration'),
    metaResolution: document.getElementById('meta-resolution'),
    metaSource: document.getElementById('meta-source'),
    detectHint: document.getElementById('detect-hint'),
    backendDot: document.getElementById('backend-dot'),
    backendText: document.getElementById('backend-text'),
    backendSub: document.getElementById('backend-sub'),
    backendHint: document.getElementById('backend-hint'),
    backendEdit: document.getElementById('backend-edit'),
    backendInput: document.getElementById('backend-input'),
    backendError: document.getElementById('backend-error'),
    btnSettings: document.getElementById('btn-settings'),
    btnRetry: document.getElementById('btn-retry'),
    btnSaveBackend: document.getElementById('btn-save-backend'),
    successCard: document.getElementById('success-card'),
    successTitle: document.getElementById('success-title'),
    successSub: document.getElementById('success-sub'),
    btnOpen: document.getElementById('btn-open'),
    btnAgain: document.getElementById('btn-again'),
    errorBox: document.getElementById('error-box'),
    errorTitle: document.getElementById('error-title'),
    errorBody: document.getElementById('error-body'),
    btnAnalyze: document.getElementById('btn-analyze'),
    btnAnalyzeLabel: document.getElementById('btn-analyze-label'),
    btnSpinner: document.getElementById('btn-spinner'),
    btnWeb: document.getElementById('btn-web'),
    actionsNote: document.getElementById('actions-note'),
    footVersion: document.getElementById('foot-version'),
  };
  var playIcon = el.btnAnalyze.querySelector('.play-icon');

  var state = {
    backendUrl: VA_DEFAULT_BACKEND,
    tab: null,
    page: null,
    detection: { status: 'detecting' },
    health: { status: 'checking' },
    submitting: false,
    success: null,
    settingsOpen: false,
    pollTimer: 0,
  };

  var healthSeq = 0;
  var saveFlashTimer = 0;
  var listenersBound = false;

  /* ---------------- 通用工具 ---------------- */

  function getExtChrome() {
    var api = globalThis.chrome;
    if (api && api.tabs && api.scripting && api.storage) return api;
    return null;
  }

  function markReady() {
    try {
      document.body.setAttribute('data-app-ready', '1');
    } catch (err) {
      /* ignore */
    }
  }

  function updateVersion() {
    var version = '';
    try {
      var api = getExtChrome();
      if (api && api.runtime && api.runtime.getManifest) {
        version = (api.runtime.getManifest() || {}).version || '';
      }
    } catch (err) {
      version = '';
    }
    el.footVersion.textContent = version ? 'v' + version : 'v1.0.0';
  }

  function setHint(container, lines, variant) {
    var list = Array.isArray(lines) ? lines.filter(Boolean) : [];
    container.hidden = list.length === 0;
    container.className = 'hint-box' + (variant === 'warn' ? ' hint-warn' : '');
    container.textContent = '';
    list.forEach(function (line) {
      var row = document.createElement('div');
      row.className = 'hint-line';
      if (typeof line === 'string') {
        row.textContent = line;
      } else {
        if (line.text) row.appendChild(document.createTextNode(line.text));
        if (line.code) {
          var code = document.createElement('code');
          code.textContent = line.code;
          row.appendChild(code);
        }
        if (line.tail) row.appendChild(document.createTextNode(line.tail));
        if (line.action) {
          var button = document.createElement('button');
          button.type = 'button';
          button.className = 'inline-action';
          button.setAttribute('data-action', line.action);
          button.textContent = line.actionLabel || '设置';
          row.appendChild(button);
        }
      }
      container.appendChild(row);
    });
  }

  function setBadge(text, variant) {
    if (!text) {
      el.detectBadge.hidden = true;
      return;
    }
    el.detectBadge.hidden = false;
    el.detectBadge.textContent = text;
    el.detectBadge.className = 'badge ' + (variant || 'badge-muted');
  }

  function setDetectIcon(mode) {
    var classMap = {
      scanning: 'detect-icon is-scanning',
      ok: 'detect-icon ok',
      warn: 'detect-icon warn',
      muted: 'detect-icon muted',
      info: 'detect-icon',
    };
    el.detectIcon.className = classMap[mode] || 'detect-icon';
  }

  function showError(title, body) {
    el.errorBox.hidden = false;
    el.errorTitle.textContent = title || '出错了';
    el.errorBody.textContent = body || '';
  }

  function hideError() {
    el.errorBox.hidden = true;
  }

  function showFieldError(message) {
    if (!message) {
      el.backendError.hidden = true;
      el.backendError.textContent = '';
      return;
    }
    el.backendError.hidden = false;
    el.backendError.textContent = message;
  }

  function hideFieldError() {
    showFieldError('');
  }

  function describeInjectError(err, url) {
    var message = String(err && err.message ? err.message : err || '');
    if (/cannot access contents|cannot access|extensions gallery|chrome:\/\/|The extensions gallery|无法访问/i.test(message)) {
      return '此页面不允许扩展读取（浏览器内置页、扩展商店、PDF 阅读器等）。请打开普通视频网页后再试。';
    }
    if (/^file:/i.test(url || '')) {
      return '本地文件页面需要先在 chrome://extensions 中开启「允许访问文件网址」，或直接把视频文件拖入网页分析界面。';
    }
    return '无法在此页面检测视频：' + (message || '未知错误');
  }

  function isScriptableUrl(url) {
    if (!url || !/^(https?|file):/i.test(url)) return false;
    try {
      var parsed = new URL(url);
      if (/chromewebstore\.google\.com$/i.test(parsed.hostname)) return false;
      return true;
    } catch (err) {
      return false;
    }
  }

  /* ---------------- 渲染 ---------------- */

  function renderPage() {
    if (state.detection.status === 'preview') {
      el.pageTitle.textContent = '扩展预览模式';
      el.pageUrl.textContent = '请从 Chrome 工具栏的扩展图标打开';
      el.pageUrl.title = '';
      return;
    }
    var title = state.page && state.page.title ? state.page.title : '';
    var url = state.page && state.page.pageUrl ? state.page.pageUrl : '';
    el.pageTitle.textContent = title || '未命名页面';
    el.pageUrl.textContent = url || '（无法读取当前页地址）';
    el.pageUrl.title = url;
  }

  function renderDetection() {
    var det = state.detection || { status: 'detecting' };
    var status = det.status || 'detecting';
    var pageUrl = state.page && state.page.pageUrl ? state.page.pageUrl : '';
    var known = isKnownVideoPage(pageUrl);

    setBadge('', 'badge-muted');
    el.metaGrid.hidden = true;
    el.detectHint.hidden = true;
    el.detectTitle.textContent = '';
    el.detectSub.textContent = '';
    el.detectSub.title = '';

    if (status === 'detecting') {
      setDetectIcon('scanning');
      el.detectTitle.textContent = '正在检测视频…';
      el.detectSub.textContent = '扫描页面中的播放器';
      return;
    }

    if (status === 'preview') {
      setDetectIcon('muted');
      el.detectTitle.textContent = '预览模式';
      el.detectSub.textContent = '请从 Chrome 工具栏的扩展图标打开';
      setHint(el.detectHint, [det.message || '预览模式下无法读取标签页。'], '');
      return;
    }

    if (status === 'found') {
      var data = det.data || {};
      setDetectIcon('ok');
      el.detectTitle.textContent = '检测到视频';
      el.detectSub.textContent = formatVideoCount(data) + (data.playing ? ' · 正在播放' : ' · 已暂停');
      if (data.isBlob) setBadge('blob 流', 'badge-warn');
      else if (known) setBadge('已知站点', 'badge-info');
      else setBadge('已检测', 'badge-ok');
      el.metaGrid.hidden = false;
      el.metaDuration.textContent = formatDuration(data.duration);
      el.metaResolution.textContent = formatResolution(data.width, data.height);
      el.metaSource.textContent = videoSourceLabel(data);
      el.metaSource.title = data.videoUrl || '';
      var lines = [];
      if (data.isBlob) {
        lines.push(known
          ? '该视频为 blob: 内嵌流，将优先按页面链接（' + knownSiteLabel(pageUrl) + '）解析。'
          : '该视频为 blob: 内嵌流，后端会尝试用页面链接解析；非已知站点时可能无法直接下载。');
      } else if (known) {
        lines.push('已知视频站点：提交后优先按页面链接解析（yt-dlp）。');
      } else if (data.videoUrl) {
        lines.push('将尝试直接下载视频直链；若失败，可在网页分析界面改用页面链接或上传文件。');
      } else {
        lines.push('未读取到视频地址，将按当前页面链接尝试解析。');
      }
      if (data.blockedFrameCount > 0) {
        lines.push('另有 ' + data.blockedFrameCount + ' 个跨域 iframe 未扫描；若视频在其中，请打开该 iframe 页面后再试。');
      }
      setHint(el.detectHint, lines, '');
      return;
    }

    if (status === 'none') {
      var noneData = det.data || {};
      setDetectIcon('muted');
      el.detectTitle.textContent = '未检测到视频';
      el.detectSub.textContent = noneData.count
        ? formatVideoCount(noneData)
        : '页面中没有可识别的 <video> 播放器';
      setBadge('无视频', 'badge-muted');
      var noneLines = [];
      if (known) {
        noneLines.push(noneData.blockedFrameCount > 0
          ? '该页面是已知视频站点；检测到跨域 iframe（视频可能在其中），可点「按页面链接分析」让后端解析页面。'
          : '该页面是已知视频站点，可点「按页面链接分析」让后端尝试解析页面。');
      } else {
        noneLines.push('可以打开网页分析界面粘贴视频链接，或下载视频后拖入上传。');
      }
      setHint(el.detectHint, noneLines, '');
      return;
    }

    setDetectIcon('warn');
    el.detectTitle.textContent = status === 'unavailable' ? '此页面无法检测' : '检测失败';
    el.detectSub.textContent = status === 'unavailable'
      ? '浏览器内置页 / 扩展商店等页面受保护'
      : '未能读取页面中的视频信息';
    setHint(el.detectHint, [det.message || '请打开视频网页后重试。'], status === 'failed' ? 'warn' : '');
  }

  function renderHealth() {
    var status = state.health.status;
    el.backendDot.className = 'status-dot ' + (status === 'online' ? 'online' : (status === 'offline' ? 'offline' : 'checking'));
    el.btnRetry.disabled = status === 'checking';
    el.backendSub.textContent = state.backendUrl;
    el.backendSub.title = state.backendUrl;

    if (status === 'online') {
      var data = state.health.data || {};
      var bits = [];
      if (data.version) bits.push('v' + data.version);
      if (data.asr && data.asr.device) bits.push('ASR ' + data.asr.device);
      bits.push(data.deepseekConfigured ? 'DeepSeek 已配置' : 'DeepSeek 未配置 Key');
      el.backendText.textContent = '本地服务已连接';
      el.backendSub.textContent = bits.join(' · ');
      el.backendHint.hidden = true;
      el.backendHint.textContent = '';
      return;
    }

    if (status === 'offline') {
      if (state.health.kind === 'http') {
        el.backendText.textContent = '本地服务响应异常';
        setHint(el.backendHint, [
          { text: '本地服务返回错误：' + (state.health.message || '未知错误') },
          { text: '请查看后端日志后点击「重试」，或', action: 'settings', actionLabel: '调整服务地址' },
        ], 'warn');
        return;
      }
      el.backendText.textContent = '本地服务未启动';
      setHint(el.backendHint, [
        { text: '请先在项目根目录运行启动脚本：', code: '.\\run.ps1' },
        { text: '启动后点击「重试」；若端口冲突可', action: 'settings', actionLabel: '调整服务地址' },
      ], 'warn');
      return;
    }

    el.backendText.textContent = '正在连接本地服务…';
    el.backendHint.hidden = true;
    el.backendHint.textContent = '';
  }

  function renderSuccess() {
    if (!state.success) {
      el.successCard.hidden = true;
      return;
    }
    var job = state.success.job || {};
    var title = job.title || (state.success.payload && state.success.payload.title) || '未命名视频';
    el.successTitle.textContent = title;
    el.successTitle.title = title;
    var idText = job.id ? '作业 #' + job.id : '作业已创建';
    el.successSub.textContent = idText + ' · ' + statusLabel(job.status) + ' · 稍后可在分析界面查看进度';
    el.successCard.hidden = false;
  }

  function healthDownNote() {
    if (state.health.status === 'checking') return '正在连接本地服务…';
    if (state.health.kind === 'http') return '本地服务响应异常，请检查后端日志后重试。';
    return '本地服务未启动，启动后自动重试。';
  }

  function renderActions() {
    el.successCard.hidden = !state.success;
    el.btnAnalyze.hidden = !!state.success;
    el.btnAnalyze.disabled = true;
    el.btnAnalyze.classList.remove('is-busy');
    el.btnSpinner.hidden = true;
    if (playIcon) playIcon.hidden = false;
    el.actionsNote.hidden = true;
    el.actionsNote.textContent = '';
    el.btnWeb.disabled = state.health.status !== 'online';

    if (state.success) return;

    if (state.submitting) {
      el.btnAnalyze.classList.add('is-busy');
      el.btnAnalyzeLabel.textContent = '正在提交…';
      el.btnSpinner.hidden = false;
      if (playIcon) playIcon.hidden = true;
      return;
    }

    var detection = state.detection || { status: 'detecting' };
    var status = detection.status;
    var pageUrl = state.page && state.page.pageUrl ? state.page.pageUrl : '';
    var known = isKnownVideoPage(pageUrl);
    var healthOk = state.health.status === 'online';
    var label = '分析这个视频';
    var enabled = false;
    var note = '';

    if (status === 'found') {
      label = '分析这个视频';
      enabled = healthOk;
      if (!healthOk) note = healthDownNote();
    } else if (status === 'none') {
      if (known) {
        label = '按页面链接分析';
        enabled = healthOk;
        if (!healthOk) note = healthDownNote();
      } else {
        label = '未检测到视频';
        note = '此页面没有可分析的视频：可打开网页分析界面粘贴视频链接，或上传本地文件。';
      }
    } else if (status === 'detecting') {
      label = '正在检测…';
    } else if (status === 'preview') {
      label = '预览模式';
    } else {
      label = '分析这个视频';
      note = detection.message || '此页面无法检测视频，请打开视频网页后重试。';
    }

    el.btnAnalyzeLabel.textContent = label;
    el.btnAnalyze.disabled = !enabled;
    el.btnAnalyze.title = enabled ? '把当前视频提交给本地分析服务' : (note || '');
    if (note) {
      el.actionsNote.hidden = false;
      el.actionsNote.textContent = note;
    }
  }

  /* ---------------- 标签页与检测 ---------------- */

  async function loadBackendUrl() {
    var api = getExtChrome();
    if (!api) return VA_DEFAULT_BACKEND;
    try {
      var stored = await api.storage.local.get(VA_BACKEND_STORAGE_KEY);
      var raw = stored ? stored[VA_BACKEND_STORAGE_KEY] : null;
      if (typeof raw === 'string' && raw.trim()) {
        try {
          return normalizeBackendUrl(raw);
        } catch (err) {
          return VA_DEFAULT_BACKEND;
        }
      }
    } catch (err) {
      /* 读取失败时使用默认地址 */
    }
    return VA_DEFAULT_BACKEND;
  }

  async function getActiveTab() {
    var api = getExtChrome();
    if (!api) return null;
    var tabs = await api.tabs.query({ active: true, currentWindow: true });
    if (!Array.isArray(tabs) || tabs.length === 0) return null;
    return tabs[0];
  }

  function pageFromTab(tab) {
    var url = tab && typeof tab.url === 'string' ? tab.url : '';
    var title = tab && typeof tab.title === 'string' ? tab.title.trim() : '';
    return { pageUrl: url, title: title };
  }

  async function detectOnTab(tab) {
    state.detection = { status: 'detecting' };
    renderDetection();
    renderActions();

    if (!tab || typeof tab.id !== 'number') {
      state.detection = { status: 'unavailable', message: '没有可检测的标签页，请在普通网页中打开扩展。' };
      renderDetection();
      renderActions();
      return;
    }
    var url = typeof tab.url === 'string' ? tab.url : '';
    if (!isScriptableUrl(url)) {
      state.detection = {
        status: 'unavailable',
        message: '当前页面不支持检测（chrome:// 等浏览器内置页面、扩展商店等受保护页面）。请打开视频网页后再试。',
      };
      renderDetection();
      renderActions();
      return;
    }

    var api = getExtChrome();
    try {
      var results = await api.scripting.executeScript({
        target: { tabId: tab.id },
        func: detectVideosInPage,
      });
      var detection = Array.isArray(results) && results.length > 0 ? results[0].result : null;
      if (!detection || detection.ok !== true) {
        state.detection = { status: 'failed', message: '检测脚本没有返回有效结果，页面可能限制了脚本注入。' };
      } else if (detection.found) {
        state.detection = { status: 'found', data: detection };
        if (!state.page || !state.page.pageUrl) {
          state.page = { pageUrl: detection.pageUrl || '', title: detection.title || '' };
          renderPage();
        }
      } else {
        state.detection = { status: 'none', data: detection };
      }
    } catch (err) {
      state.detection = { status: 'failed', message: describeInjectError(err, url) };
    }
    renderDetection();
    renderActions();
  }

  /* ---------------- 后端连接 ---------------- */

  function stopHealthPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = 0;
    }
  }

  function startHealthPolling() {
    stopHealthPolling();
    if (state.health.status === 'online' || state.success) return;
    var attempts = 0;
    var tick = async function () {
      state.pollTimer = 0;
      if (state.success || state.health.status === 'online') return;
      attempts += 1;
      if (attempts > 20) return;
      var seq = healthSeq;
      var result = await backendHealth(state.backendUrl, 2500);
      if (seq !== healthSeq) return;
      if (result.ok) {
        state.health = { status: 'online', data: result };
        renderHealth();
        renderActions();
        return;
      }
      if (attempts < 20) state.pollTimer = setTimeout(tick, 3000);
    };
    state.pollTimer = setTimeout(tick, 3000);
  }

  async function refreshHealth() {
    var seq = ++healthSeq;
    state.health = { status: 'checking' };
    renderHealth();
    renderActions();
    var result = await backendHealth(state.backendUrl, 2500);
    if (seq !== healthSeq) return;
    if (result.ok) {
      state.health = { status: 'online', data: result };
      stopHealthPolling();
    } else {
      state.health = { status: 'offline', message: result.message, kind: result.kind };
    }
    renderHealth();
    renderActions();
    if (!result.ok && !state.success) startHealthPolling();
  }

  /* ---------------- 操作 ---------------- */

  function toggleSettings(force) {
    var open = typeof force === 'boolean' ? force : !state.settingsOpen;
    state.settingsOpen = open;
    el.backendEdit.hidden = !open;
    el.btnSettings.setAttribute('aria-expanded', String(open));
    hideFieldError();
    if (open) {
      el.backendInput.value = state.backendUrl;
      try {
        el.backendInput.focus();
        el.backendInput.select();
      } catch (err) {
        /* ignore */
      }
    }
  }

  function flashSaved() {
    if (saveFlashTimer) clearTimeout(saveFlashTimer);
    el.btnSaveBackend.textContent = '已保存';
    saveFlashTimer = setTimeout(function () {
      el.btnSaveBackend.textContent = '保存';
      saveFlashTimer = 0;
    }, 1400);
  }

  async function handleSaveBackend() {
    hideFieldError();
    var normalized;
    try {
      normalized = normalizeBackendUrl(el.backendInput.value);
    } catch (err) {
      showFieldError(err.message);
      return;
    }
    var api = getExtChrome();
    if (!api) {
      showFieldError('预览模式下无法保存');
      return;
    }
    try {
      await api.storage.local.set({ [VA_BACKEND_STORAGE_KEY]: normalized });
    } catch (err) {
      showFieldError('保存失败：' + (err && err.message ? err.message : err));
      return;
    }
    var changed = normalized !== state.backendUrl;
    state.backendUrl = normalized;
    el.backendInput.value = normalized;
    el.backendSub.textContent = normalized;
    flashSaved();
    if (changed) {
      healthSeq += 1;
      stopHealthPolling();
      await refreshHealth();
    } else {
      renderHealth();
    }
  }

  async function handleRetry() {
    hideError();
    if (state.success) {
      resetAfterSuccess();
    }
    var tasks = [refreshHealth()];
    if (state.tab) tasks.push(detectOnTab(state.tab));
    await Promise.all(tasks);
  }

  async function handleAnalyzeClick() {
    if (state.submitting || state.success) return;
    if (state.health.status !== 'online') {
      showError('本地服务未启动', '请先在项目根目录运行 run.ps1，然后点击「重试」。');
      toggleSettings(true);
      return;
    }
    var page = state.page || {};
    var detection = state.detection.status === 'found' ? state.detection.data : null;
    if (!page.pageUrl) {
      showError('无法获取页面地址', '请在普通网页中打开扩展后重试。');
      return;
    }
    if (!detection && !isKnownVideoPage(page.pageUrl)) {
      showError('此页面没有可分析的视频', '请打开包含视频的网页，或使用网页分析界面粘贴视频链接 / 上传本地文件。');
      return;
    }

    var payload;
    try {
      payload = buildTabJobPayload(page, detection);
    } catch (err) {
      showError('无法提交', err.message);
      return;
    }

    hideError();
    state.submitting = true;
    renderActions();
    var result = await submitTabJob(state.backendUrl, payload, 20000);
    state.submitting = false;
    if (result.ok) {
      state.success = { job: result.job, payload: payload };
      renderActions();
      renderSuccess();
      stopHealthPolling();
      return;
    }
    renderActions();
    if (result.kind === 'network' || result.kind === 'timeout') {
      state.health = { status: 'offline', message: result.message };
      renderHealth();
      renderActions();
      showError('提交失败', result.message);
      startHealthPolling();
      return;
    }
    showError('提交失败', result.message);
  }

  async function openBackendUi() {
    if (state.health.status !== 'online') {
      showError('本地服务未启动', '请先在项目根目录运行 run.ps1，启动后再打开分析界面。');
      toggleSettings(true);
      return;
    }
    var url = analysisUrl(state.backendUrl);
    var api = getExtChrome();
    if (api && api.tabs && api.tabs.create) {
      try {
        await api.tabs.create({ url: url });
        return;
      } catch (err) {
        /* 回退到 window.open */
      }
    }
    window.open(url, '_blank', 'noopener');
  }

  function resetAfterSuccess() {
    if (!state.success) return;
    state.success = null;
    hideError();
    renderSuccess();
    renderActions();
    if (state.health.status !== 'online') startHealthPolling();
  }

  /* ---------------- 初始化 ---------------- */

  async function boot() {
    stopHealthPolling();
    healthSeq += 1;
    state.backendUrl = VA_DEFAULT_BACKEND;
    state.tab = null;
    state.page = null;
    state.detection = { status: 'detecting' };
    state.health = { status: 'checking' };
    state.submitting = false;
    state.success = null;
    state.settingsOpen = false;
    state.pollTimer = 0;
    hideError();
    hideFieldError();
    el.backendEdit.hidden = true;
    el.btnSettings.setAttribute('aria-expanded', 'false');
    el.successCard.hidden = true;
    el.backendInput.value = state.backendUrl;
    el.backendSub.textContent = state.backendUrl;
    updateVersion();
    renderPage();
    renderDetection();
    renderHealth();
    renderSuccess();
    renderActions();

    var api = getExtChrome();
    if (!api) {
      state.detection = {
        status: 'preview',
        message: '预览模式：当前页面没有扩展 API。请从 Chrome 工具栏的扩展图标打开本页面。',
      };
      renderPage();
      renderDetection();
      renderActions();
      await refreshHealth();
      markReady();
      return;
    }

    state.backendUrl = await loadBackendUrl();
    el.backendInput.value = state.backendUrl;
    el.backendSub.textContent = state.backendUrl;

    try {
      state.tab = await getActiveTab();
    } catch (err) {
      state.tab = null;
    }
    state.page = pageFromTab(state.tab);
    renderPage();

    await Promise.all([refreshHealth(), detectOnTab(state.tab)]);
    markReady();
  }

  function bindListeners() {
    if (listenersBound) return;
    listenersBound = true;
    el.btnSettings.addEventListener('click', function () {
      toggleSettings();
    });
    el.btnRetry.addEventListener('click', handleRetry);
    el.btnSaveBackend.addEventListener('click', handleSaveBackend);
    el.backendInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        handleSaveBackend();
      }
    });
    el.btnAnalyze.addEventListener('click', handleAnalyzeClick);
    el.btnOpen.addEventListener('click', openBackendUi);
    el.btnWeb.addEventListener('click', openBackendUi);
    el.btnAgain.addEventListener('click', resetAfterSuccess);
    document.addEventListener('click', function (event) {
      var target = event.target && event.target.closest ? event.target.closest('[data-action]') : null;
      if (!target) return;
      if (target.getAttribute('data-action') === 'settings') toggleSettings(true);
    });
    window.addEventListener('unload', stopHealthPolling);
  }

  function safeBoot() {
    boot().catch(function (err) {
      console.error('popup 初始化失败', err);
      markReady();
    });
  }

  bindListeners();
  window.VA_BOOT = { boot: boot, state: state };

  // #manual：供 dev/preview.html 先注入 chrome 打桩再手动引导（扩展正常加载不会命中）
  if (location.hash.indexOf('manual') === -1) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', safeBoot);
    } else {
      safeBoot();
    }
  }
})();
