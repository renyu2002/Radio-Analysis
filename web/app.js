/*!
 * app.js —— 视频分析 · DeepSeek 前端 SPA
 * 原生 JS，无构建步骤、无外部依赖；所有接口按 docs/CONTRACT.md §8 调用。
 */
(function () {
  'use strict';

  /* =======================================================================
     工具函数
     ======================================================================= */

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function escapeHtml(value) {
    if (window.Markdown && Markdown.escapeHtml) return Markdown.escapeHtml(value);
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  /** 秒 → mm:ss（超过 1 小时自动变为 h:mm:ss） */
  function fmtTime(seconds) {
    var total = Math.floor(Number(seconds));
    if (!isFinite(total) || total < 0) total = 0;
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    var mm = (m < 10 ? '0' : '') + m;
    var ss = (s < 10 ? '0' : '') + s;
    return h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
  }

  /** 秒 → SRT 时间戳 00:00:00,000 */
  function fmtSrtTime(seconds) {
    var ms = Math.round((Number(seconds) || 0) * 1000);
    if (ms < 0) ms = 0;
    var h = Math.floor(ms / 3600000);
    var m = Math.floor((ms % 3600000) / 60000);
    var s = Math.floor((ms % 60000) / 1000);
    var rest = ms % 1000;
    return pad2(h) + ':' + pad2(m) + ':' + pad2(s) + ',' + String(rest).padStart(3, '0');
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function fmtDate(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var now = new Date();
    var base = pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    return d.getFullYear() === now.getFullYear() ? base : d.getFullYear() + '-' + base;
  }

  function clampPct(value) {
    var n = Number(value);
    if (!isFinite(n)) return 0;
    return Math.max(0, Math.min(100, Math.round(n * 100)));
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      var self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }

  function safeFilename(name, ext) {
    var base = String(name || '视频分析')
      .replace(/[\\/:*?"<>|\r\n\t]+/g, '_')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 80);
    if (!base) base = '视频分析';
    return base + '.' + ext;
  }

  function sourceLabel(type) {
    if (type === 'url') return '网页链接';
    if (type === 'tab') return '浏览器标签页';
    return '本地文件';
  }

  function statusInfo(job) {
    switch (job.status) {
      case 'pending': return { label: '等待中', cls: 'is-pending' };
      case 'running': return { label: '处理中', cls: 'is-running' };
      case 'done': return { label: '已完成', cls: 'is-done' };
      case 'error': return { label: '失败', cls: 'is-error' };
      default: return { label: job.status || '未知', cls: 'is-pending' };
    }
  }

  function statusBadgeHtml(job) {
    var info = statusInfo(job);
    return '<span class="status-badge ' + info.cls + '">' + info.label + '</span>';
  }

  function isActive(job) {
    return !!job && (job.status === 'pending' || job.status === 'running');
  }

  function canChat(job) {
    return !!job && job.status === 'done' && !!job.has_transcript;
  }

  function titleFromFilename(name) {
    var base = String(name || '').replace(/\.[^.]+$/, '').trim();
    return base || String(name || '未命名视频');
  }

  /* =======================================================================
     全局状态
     ======================================================================= */

  var state = {
    health: null,
    wasOnline: false,
    settings: null,
    jobs: [],
    listTimer: null,
    healthTimer: null,
    focusTimer: null,
    currentJobId: null,
    job: null,
    detailToken: 0,
    pollTimer: null,
    activeTab: 'summary',
    data: {},
    transcriptFilter: '',
    transcriptFiltered: [],
    transcriptShown: 0,
    transcriptObserver: null,
    currentSegmentId: null,
    chat: null,
    pendingSeek: null,
    playerTimer: null,
    retryFile: null,
    uploadXhr: null,
    openModals: [],
    confirmResolver: null,
    exportOpen: false
  };

  /* =======================================================================
     API 封装
     ======================================================================= */

  function ApiError(message, status) {
    this.name = 'ApiError';
    this.message = message || '请求失败';
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      headers: Object.assign({ 'Accept': 'application/json' }, opts.headers || {})
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (text) {
          var message = '请求失败（HTTP ' + res.status + '）';
          if (text) {
            try {
              var parsed = JSON.parse(text);
              if (parsed && parsed.detail) {
                message = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail);
              }
            } catch (e) {
              message = text.slice(0, 300) || message;
            }
          }
          throw new ApiError(message, res.status);
        });
      }
      if (res.status === 204) return null;
      var ct = res.headers.get('content-type') || '';
      if (ct.indexOf('application/json') === -1) return null;
      return res.json().catch(function () { return null; });
    }, function () {
      throw new ApiError('无法连接本地服务，请确认后端已启动（默认 http://127.0.0.1:8765）。', 0);
    });
  }

  /* =======================================================================
     DOM 引用
     ======================================================================= */

  var dom = {
    statusPill: $('#statusPill'),
    statusText: $('#statusText'),
    brandVersion: $('#brandVersion'),
    btnSettings: $('#btnSettings'),
    offlineHint: $('#offlineHint'),

    dropzone: $('#dropzone'),
    fileInput: $('#fileInput'),
    uploadProgress: $('#uploadProgress'),
    uploadName: $('#uploadName'),
    uploadPct: $('#uploadPct'),
    uploadBar: $('#uploadBar'),
    uploadHint: $('#uploadHint'),
    btnCancelUpload: $('#btnCancelUpload'),
    urlInput: $('#urlInput'),
    btnAnalyzeUrl: $('#btnAnalyzeUrl'),

    jobCount: $('#jobCount'),
    btnRefreshJobs: $('#btnRefreshJobs'),
    jobList: $('#jobList'),

    emptyState: $('#emptyState'),
    content: $('#content'),
    btnEmptyUpload: $('#btnEmptyUpload'),
    btnEmptySettings: $('#btnEmptySettings'),
    loadingState: $('#loadingState'),

    jobView: $('#jobView'),
    jobTitle: $('#jobTitle'),
    jobStatusBadge: $('#jobStatusBadge'),
    jobSourceBadge: $('#jobSourceBadge'),
    jobMeta: $('#jobMeta'),
    btnRegenerate: $('#btnRegenerate'),
    btnDeleteJob: $('#btnDeleteJob'),
    errorBanner: $('#errorBanner'),
    errorMessage: $('#errorMessage'),
    btnRetryJob: $('#btnRetryJob'),
    stageStrip: $('#stageStrip'),

    playerWrap: $('#playerWrap'),
    video: $('#video'),
    playerPlaceholder: $('#playerPlaceholder'),
    playerProcessing: $('#playerProcessing'),
    playerProcessingText: $('#playerProcessingText'),

    tabPanels: {
      summary: $('#tab-summary'),
      transcript: $('#tab-transcript'),
      ocr: $('#tab-ocr'),
      chat: $('#tab-chat')
    },
    transcriptTabCount: $('#transcriptTabCount'),
    ocrTabCount: $('#ocrTabCount'),

    chatLog: $('#chatLog'),
    chatSuggest: $('#chatSuggest'),
    chatInput: $('#chatInput'),
    btnSend: $('#btnSend'),
    chatHintText: $('#chatHintText'),

    settingsModal: $('#settingsModal'),
    setApiKey: $('#setApiKey'),
    btnToggleKey: $('#btnToggleKey'),
    keyEyeIcon: $('#keyEyeIcon'),
    keyHint: $('#keyHint'),
    setBaseUrl: $('#setBaseUrl'),
    setModel: $('#setModel'),
    setModelCustom: $('#setModelCustom'),
    setWhisperModel: $('#setWhisperModel'),
    setDevice: $('#setDevice'),
    deviceInfo: $('#deviceInfo'),
    setVisionEnabled: $('#setVisionEnabled'),
    setVisionBaseUrl: $('#setVisionBaseUrl'),
    setVisionModel: $('#setVisionModel'),
    setVisionApiKey: $('#setVisionApiKey'),
    visionKeyHint: $('#visionKeyHint'),
    testResult: $('#testResult'),
    btnTestKey: $('#btnTestKey'),
    btnSaveSettings: $('#btnSaveSettings'),

    confirmModal: $('#confirmModal'),
    confirmTitle: $('#confirmTitle'),
    confirmMessage: $('#confirmMessage'),
    btnConfirmCancel: $('#btnConfirmCancel'),
    btnConfirmOk: $('#btnConfirmOk'),

    lightbox: $('#lightbox'),
    lightboxImg: $('#lightboxImg'),
    lightboxTime: $('#lightboxTime'),
    lightboxText: $('#lightboxText'),

    toasts: $('#toasts')
  };

  /* =======================================================================
     Toast / 弹窗
     ======================================================================= */

  var TOAST_ICONS = { success: '#i-check', error: '#i-alert', info: '#i-spark' };

  function toast(message, type, duration) {
    if (!dom.toasts) return;
    var kind = type || 'info';
    var node = document.createElement('div');
    node.className = 'toast toast-' + kind;
    node.innerHTML = '<svg class="icon"><use href="' + (TOAST_ICONS[kind] || '#i-spark') + '"/></svg><span></span>';
    node.querySelector('span').textContent = String(message == null ? '' : message);
    dom.toasts.appendChild(node);
    while (dom.toasts.children.length > 4) dom.toasts.firstElementChild.remove();
    requestAnimationFrame(function () { node.classList.add('show'); });
    setTimeout(function () {
      node.classList.remove('show');
      setTimeout(function () { node.remove(); }, 240);
    }, duration || 3600);
  }

  function openModal(el) {
    if (!el || state.openModals.indexOf(el) !== -1) return;
    el.classList.remove('is-hidden');
    state.openModals.push(el);
    document.body.classList.add('modal-open');
  }

  function closeModal(el) {
    if (!el) return;
    el.classList.add('is-hidden');
    state.openModals = state.openModals.filter(function (item) { return item !== el; });
    if (el === dom.confirmModal && state.confirmResolver) {
      var resolve = state.confirmResolver;
      state.confirmResolver = null;
      resolve(false);
    }
    if (!state.openModals.length) document.body.classList.remove('modal-open');
  }

  function closeTopModal() {
    if (!state.openModals.length) return;
    closeModal(state.openModals[state.openModals.length - 1]);
  }

  function closeByAction(name) {
    if (name === 'settings') closeModal(dom.settingsModal);
    else if (name === 'confirm') closeModal(dom.confirmModal);
    else if (name === 'lightbox') closeModal(dom.lightbox);
  }

  function confirmDialog(opts) {
    var options = opts || {};
    if (state.confirmResolver) {
      state.confirmResolver(false);
      state.confirmResolver = null;
    }
    dom.confirmTitle.textContent = options.title || '确认操作';
    dom.confirmMessage.textContent = options.message || '';
    dom.btnConfirmOk.textContent = options.okText || '确认';
    dom.btnConfirmOk.classList.toggle('danger', !!options.danger);
    openModal(dom.confirmModal);
    setTimeout(function () { dom.btnConfirmOk.focus(); }, 40);
    return new Promise(function (resolve) { state.confirmResolver = resolve; });
  }

  function openSettings() {
    loadSettings().then(function () {
      fillSettingsForm();
      dom.testResult.className = 'test-result';
      dom.testResult.textContent = '';
      openModal(dom.settingsModal);
    });
  }

  function openLightbox(frame, url) {
    dom.lightboxImg.src = url;
    dom.lightboxImg.alt = '画面帧预览';
    dom.lightboxTime.textContent = fmtTime(frame.time);
    dom.lightboxTime.dataset.seek = String(frame.time);
    dom.lightboxText.textContent = frame.text || '（该画面没有识别到文字）';
    openModal(dom.lightbox);
  }

  /* =======================================================================
     后端健康状态
     ======================================================================= */

  function setStatusPill(online, health) {
    dom.statusPill.classList.toggle('is-online', !!online);
    dom.statusPill.classList.toggle('is-offline', !online);
    dom.statusText.textContent = online ? '服务正常' : '后端未连接';
    if (online && health) {
      var asr = health.asr || {};
      var device = String(asr.device || 'unknown').toUpperCase();
      var parts = ['后端已连接', 'v' + (health.version || '?'), device];
      if (asr.model) parts.push(asr.model + (asr.compute_type ? ' · ' + asr.compute_type : ''));
      parts.push(health.deepseek_configured ? 'DeepSeek 已配置' : 'DeepSeek 未配置');
      dom.statusPill.title = parts.join(' · ');
      if (health.version) dom.brandVersion.textContent = 'v' + health.version;
    } else {
      dom.statusPill.title = '后端未连接，点击重新检测';
    }
    if (dom.offlineHint) dom.offlineHint.classList.toggle('is-hidden', !!online);
  }

  function refreshHealth(options) {
    var opts = options || {};
    var wasOnline = state.wasOnline === true;
    return api('/api/health').then(function (health) {
      state.health = health || {};
      state.wasOnline = true;
      setStatusPill(true, state.health);
      if (!wasOnline && opts.autoLoad !== false) {
        return Promise.all([loadSettings(), loadJobs()]).then(function () {
          openJobFromHashIfNeeded();
        });
      }
      return null;
    }, function () {
      state.health = null;
      state.wasOnline = false;
      setStatusPill(false);
      return null;
    });
  }

  function checkHealthManual() {
    refreshHealth().then(function () {
      if (state.wasOnline) toast('后端连接正常', 'success', 2200);
      else toast('仍无法连接后端，请确认服务已启动', 'error', 4000);
    });
  }

  /* =======================================================================
     设置
     ======================================================================= */

  function loadSettings() {
    return api('/api/settings').then(function (data) {
      if (data && data.settings) state.settings = data.settings;
      return state.settings;
    }, function () {
      return state.settings;
    });
  }

  function setSelectValue(select, value, fallback) {
    var target = value || fallback;
    if (!target) return;
    var exists = $$('option', select).some(function (opt) { return opt.value === target; });
    if (!exists) {
      var opt = document.createElement('option');
      opt.value = target;
      opt.textContent = target;
      select.insertBefore(opt, select.firstChild);
    }
    select.value = target;
  }

  function fillSettingsForm() {
    var s = state.settings || {};
    var hasKey = !!s.deepseek_api_key_set;
    dom.setApiKey.value = '';
    dom.setApiKey.type = 'password';
    dom.setApiKey.placeholder = hasKey
      ? '已配置（' + (s.deepseek_api_key_masked || '****') + '），留空则不修改'
      : 'sk-…（尚未配置，可留空使用环境变量 DEEPSEEK_API_KEY）';
    dom.keyEyeIcon.querySelector('use').setAttribute('href', '#i-eye');
    dom.keyHint.textContent = hasKey
      ? '已配置：' + (s.deepseek_api_key_masked || '已设置')
      : '尚未配置。未配置时仍可完成转写与画面识别，总结会降级为转写节选。';
    dom.keyHint.classList.toggle('is-warn', !hasKey);

    dom.setBaseUrl.value = s.deepseek_base_url || 'https://api.deepseek.com';

    var model = s.deepseek_model || 'deepseek-chat';
    var knownModels = ['deepseek-chat', 'deepseek-reasoner'];
    if (knownModels.indexOf(model) !== -1) {
      dom.setModel.value = model;
      dom.setModelCustom.value = '';
      dom.setModelCustom.classList.add('is-hidden');
    } else {
      dom.setModel.value = '__custom__';
      dom.setModelCustom.value = model;
      dom.setModelCustom.classList.remove('is-hidden');
    }

    setSelectValue(dom.setWhisperModel, s.whisper_model, 'large-v3');
    setSelectValue(dom.setDevice, s.whisper_device, 'auto');

    dom.setVisionEnabled.checked = !!s.vision_enabled;
    dom.setVisionBaseUrl.value = s.vision_base_url || '';
    dom.setVisionModel.value = s.vision_model || '';
    dom.setVisionApiKey.value = '';
    dom.setVisionApiKey.placeholder = s.vision_api_key_set ? '已配置，留空则不修改' : '未配置';
    dom.visionKeyHint.textContent = s.vision_api_key_set ? '已配置' : '未配置（启用视觉描述时需要填写）';

    renderDeviceInfo();
  }

  function renderDeviceInfo() {
    var health = state.health;
    var asr = (health && health.asr) || {};
    var deviceInfo = dom.deviceInfo;
    if (!deviceInfo) return;
    if (health && asr.device) {
      deviceInfo.innerHTML = '<svg class="icon"><use href="#i-spark"/></svg><span>后端当前设备：<strong>' +
        escapeHtml(String(asr.device).toUpperCase()) + '</strong>' +
        (asr.compute_type ? '（' + escapeHtml(asr.compute_type) + '）' : '') +
        (asr.model ? ' · 模型 ' + escapeHtml(asr.model) : '') +
        (asr.cuda_available ? '' : ' · 未检测到可用 CUDA') + '</span>';
    } else {
      deviceInfo.innerHTML = '<svg class="icon"><use href="#i-alert"/></svg><span>后端未连接，无法获取当前设备信息</span>';
    }
  }

  function buildSettingsPayload() {
    var s = state.settings || {};
    var payload = {};

    var apiKey = dom.setApiKey.value.trim();
    if (apiKey) payload.deepseek_api_key = apiKey;

    var baseUrl = dom.setBaseUrl.value.trim() || 'https://api.deepseek.com';
    if (baseUrl !== s.deepseek_base_url) payload.deepseek_base_url = baseUrl;

    var model;
    if (dom.setModel.value === '__custom__') {
      model = dom.setModelCustom.value.trim();
      if (!model) {
        toast('请填写自定义模型名', 'error');
        return null;
      }
    } else {
      model = dom.setModel.value;
    }
    if (model && model !== s.deepseek_model) payload.deepseek_model = model;

    if (dom.setWhisperModel.value && dom.setWhisperModel.value !== s.whisper_model) payload.whisper_model = dom.setWhisperModel.value;
    if (dom.setDevice.value && dom.setDevice.value !== s.whisper_device) payload.whisper_device = dom.setDevice.value;

    payload.vision_enabled = !!dom.setVisionEnabled.checked;
    var visionBase = dom.setVisionBaseUrl.value.trim();
    if (visionBase !== (s.vision_base_url || '')) payload.vision_base_url = visionBase;
    var visionModel = dom.setVisionModel.value.trim();
    if (visionModel !== (s.vision_model || '')) payload.vision_model = visionModel;
    var visionKey = dom.setVisionApiKey.value.trim();
    if (visionKey) payload.vision_api_key = visionKey;

    return payload;
  }

  function saveSettings(silent) {
    var payload = buildSettingsPayload();
    if (payload === null) return Promise.resolve(false);
    if (!Object.keys(payload).length) {
      if (!silent) toast('没有需要保存的修改', 'info', 2400);
      return Promise.resolve(true);
    }
    dom.btnSaveSettings.disabled = true;
    return api('/api/settings', { method: 'PUT', body: payload }).then(function (data) {
      if (data && data.settings) state.settings = data.settings;
      fillSettingsForm();
      refreshHealth({ autoLoad: false });
      if (!silent) toast('设置已保存', 'success');
      if (state.job && state.activeTab === 'summary') renderSummaryPanel(true);
      if (state.job && state.activeTab === 'chat') { renderChatSuggestions(); updateComposer(); }
      dom.btnSaveSettings.disabled = false;
      return true;
    }, function (err) {
      dom.btnSaveSettings.disabled = false;
      if (!silent) toast('保存失败：' + err.message, 'error', 4800);
      return false;
    });
  }

  function setTestResult(kind, message) {
    dom.testResult.className = 'test-result is-' + kind;
    dom.testResult.innerHTML = '<svg class="icon"><use href="' +
      (kind === 'ok' ? '#i-check' : kind === 'error' ? '#i-alert' : '#i-refresh') + '"/></svg><span></span>';
    dom.testResult.querySelector('span').textContent = message;
  }

  function testConnection() {
    dom.btnTestKey.disabled = true;
    var previous = dom.btnTestKey.textContent;
    dom.btnTestKey.textContent = '测试中…';
    setTestResult('testing', '正在保存设置并测试 DeepSeek 连接…');
    saveSettings(true).then(function (saved) {
      if (!saved) throw new ApiError('设置保存失败，无法测试', 0);
      return api('/api/settings/test-deepseek', { method: 'POST', body: {} });
    }).then(function (res) {
      if (res && res.ok) {
        var message = res.message || ('连接成功' + (res.model ? '（' + res.model + '）' : ''));
        setTestResult('ok', message);
        toast(message, 'success', 4200);
      } else {
        var failMessage = (res && (res.message || res.detail)) || '连接失败，请检查 API Key 与 Base URL';
        setTestResult('error', failMessage);
        toast(failMessage, 'error', 5000);
      }
    }).catch(function (err) {
      setTestResult('error', err.message);
      toast('测试失败：' + err.message, 'error', 4800);
    }).then(function () {
      dom.btnTestKey.disabled = false;
      dom.btnTestKey.textContent = previous || '测试连接';
    });
  }

  /* =======================================================================
     作业列表
     ======================================================================= */

  function scheduleListRefresh() {
    clearTimeout(state.listTimer);
    state.listTimer = null;
    var hasActive = state.jobs.some(isActive);
    if (!hasActive) return;
    state.listTimer = setTimeout(function () { loadJobs(); }, 4000);
  }

  function loadJobs() {
    return api('/api/jobs?limit=50').then(function (data) {
      state.jobs = (data && Array.isArray(data.jobs)) ? data.jobs : [];
      renderJobList();
      scheduleListRefresh();
      return true;
    }, function (err) {
      if (err.status === 404) {
        state.jobs = [];
        renderJobList();
        return false;
      }
      renderJobListError(err);
      return false;
    });
  }

  function buildJobItem(job) {
    var item = document.createElement('div');
    item.className = 'job-item' + (job.id === state.currentJobId ? ' is-active' : '');
    item.dataset.id = job.id;
    item.tabIndex = 0;
    item.setAttribute('role', 'button');
    var info = statusInfo(job);
    var pct = clampPct(job.progress);
    var duration = job.duration ? fmtTime(job.duration) : '';
    var active = isActive(job);
    var html = '<div class="job-item-head">' +
      '<span class="status-badge ' + info.cls + '">' + info.label + '</span>' +
      (duration ? '<span class="job-duration">' + duration + '</span>' : '<span class="job-duration"></span>') +
      '<button class="icon-btn job-del" type="button" title="删除作业" aria-label="删除作业">' +
      '<svg class="icon"><use href="#i-trash"/></svg></button>' +
      '</div>' +
      '<div class="job-item-title">' + escapeHtml(job.title || '未命名视频') + '</div>' +
      '<div class="job-item-sub"><span>' + escapeHtml(sourceLabel(job.source_type)) + '</span><span>·</span><span>' +
      escapeHtml(fmtDate(job.created_at) || '刚刚') + '</span>' +
      (job.segment_count ? '<span>·</span><span>' + job.segment_count + ' 段</span>' : '') +
      '</div>';
    if (active) {
      html += '<div class="job-progress"><div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="job-progress-text"><span>' + escapeHtml(job.stage_label || '处理中') + '</span>' +
        '<span class="pct">' + pct + '%</span></div></div>';
    }
    item.innerHTML = html;

    item.addEventListener('click', function (ev) {
      if (ev.target.closest('.job-del')) return;
      openJob(job.id);
    });
    item.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openJob(job.id);
      }
    });
    item.querySelector('.job-del').addEventListener('click', function (ev) {
      ev.stopPropagation();
      deleteJob(job);
    });
    return item;
  }

  function renderJobList() {
    if (!dom.jobList) return;
    var scrollTop = dom.jobList.scrollTop;
    dom.jobCount.textContent = String(state.jobs.length);
    dom.jobList.innerHTML = '';
    if (!state.jobs.length) {
      var empty = document.createElement('div');
      empty.className = 'list-empty';
      empty.innerHTML = '还没有作业<br>拖入视频或粘贴链接开始分析';
      dom.jobList.appendChild(empty);
      return;
    }
    var frag = document.createDocumentFragment();
    state.jobs.forEach(function (job) { frag.appendChild(buildJobItem(job)); });
    dom.jobList.appendChild(frag);
    dom.jobList.scrollTop = scrollTop;
  }

  function renderJobListError(err) {
    if (!dom) return;
    dom.jobCount.textContent = '0';
    dom.jobList.innerHTML = '<div class="list-error">' +
      '<div>' + escapeHtml(err && err.message ? err.message : '无法载入作业列表') + '</div>' +
      '<button class="btn btn-ghost" type="button" data-action="retry-list">重新载入</button>' +
      '</div>';
  }

  function upsertJobInList(job) {
    if (!job) return;
    var found = false;
    for (var i = 0; i < state.jobs.length; i++) {
      if (state.jobs[i].id === job.id) { state.jobs[i] = job; found = true; break; }
    }
    if (!found) state.jobs.unshift(job);
    renderJobList();
    scheduleListRefresh();
  }

  function updateActiveListItem() {
    $$('.job-item', dom.jobList).forEach(function (item) {
      var active = item.dataset.id === state.currentJobId;
      item.classList.toggle('is-active', active);
      if (active && state.currentJobId) item.scrollIntoView({ block: 'nearest' });
    });
  }

  /* =======================================================================
     视图切换 / 路由
     ======================================================================= */

  function setView(name) {
    dom.emptyState.classList.toggle('is-hidden', name !== 'empty');
    dom.jobView.classList.toggle('is-hidden', name !== 'job');
    dom.loadingState.classList.toggle('is-hidden', name !== 'loading');
  }

  function parseHashJobId() {
    var hash = location.hash || '';
    var match = hash.match(/^#\/?job\/([^/?#]+)/);
    if (!match) return null;
    try { return decodeURIComponent(match[1]); } catch (e) { return match[1]; }
  }

  function setHashJob(id) {
    var target = '#/job/' + encodeURIComponent(id);
    if (location.hash !== target) location.hash = target;
  }

  function clearHash() {
    if (location.hash) {
      try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { location.hash = ''; }
    }
  }

  function openJobFromHashIfNeeded() {
    var id = parseHashJobId();
    if (id && !state.job) openJob(id, { fromHash: true });
  }

  /* =======================================================================
     作业详情
     ======================================================================= */

  function resetJobData() {
    state.data = {};
    state.transcriptFilter = '';
    state.transcriptFiltered = [];
    state.transcriptShown = 0;
    state.currentSegmentId = null;
    state.pendingSeek = null;
    if (state.transcriptObserver) { state.transcriptObserver.disconnect(); state.transcriptObserver = null; }
    state.chat = {
      loaded: false,
      streaming: false,
      controller: null,
      messages: [],
      suggestions: []
    };
    if (dom.chatLog) dom.chatLog.innerHTML = '';
    if (dom.chatSuggest) dom.chatSuggest.innerHTML = '';
  }

  function resetPlayer() {
    var video = dom.video;
    if (!video) return;
    clearPlayerTimer();
    try { video.pause(); } catch (e) { /* noop */ }
    dom.playerWrap.classList.remove('video-ready');
    video.removeAttribute('src');
    video.removeAttribute('poster');
    try { video.load(); } catch (e) { /* noop */ }
    setPlayerPlaceholder('static', '暂无视频');
    dom.playerProcessing.classList.add('is-hidden');
  }

  function setPlayerPlaceholder(kind, text) {
    var ph = dom.playerPlaceholder;
    if (kind === 'hidden') {
      ph.classList.add('is-hidden');
      return;
    }
    ph.classList.remove('is-hidden');
    ph.classList.toggle('is-static', kind === 'static');
    if (kind === 'processing') {
      ph.innerHTML = '<div class="spinner"></div><p></p>';
    } else {
      ph.innerHTML = '<svg class="icon"><use href="#i-file-video"/></svg><p></p>';
    }
    ph.querySelector('p').textContent = text || '';
  }

  function isMediaEligible(job) {
    if (!job) return false;
    if (job.status === 'done') return true;
    if (job.status !== 'running') return false;
    var stage = String(job.stage || '');
    return ['transcribing', 'frames', 'ocr', 'analyzing', 'done'].indexOf(stage) !== -1;
  }

  function updatePlayer(job) {
    var mediaUrl = job.media_url || '';
    if (mediaUrl && !isMediaEligible(job)) mediaUrl = '';
    var video = dom.video;
    if (mediaUrl) {
      if (video.getAttribute('src') !== mediaUrl) {
        video.src = mediaUrl;
        state.pendingSeek = null;
        if (job.thumbnail_url) video.poster = job.thumbnail_url;
        else video.removeAttribute('poster');
        dom.playerWrap.classList.remove('video-ready');
        setPlayerPlaceholder('processing', '正在载入视频…');
        startPlayerTimer(job);
      } else if (job.thumbnail_url && video.getAttribute('poster') !== job.thumbnail_url) {
        video.poster = job.thumbnail_url;
      }
    } else {
      if (video.getAttribute('src')) resetPlayer();
      if (isActive(job)) {
        setPlayerPlaceholder('processing', '视频处理中：' + (job.stage_label || '准备中') + ' ' + clampPct(job.progress) + '%');
      } else {
        setPlayerPlaceholder('static', job.status === 'error' ? '没有可用的视频文件' : '视频文件不可用');
      }
    }
    var active = isActive(job);
    dom.playerProcessing.classList.toggle('is-hidden', !(active && !!mediaUrl));
    if (active) dom.playerProcessingText.textContent = (job.stage_label || '处理中') + ' ' + clampPct(job.progress) + '%';
  }

  function clearPlayerTimer() {
    if (state.playerTimer) {
      clearTimeout(state.playerTimer);
      state.playerTimer = null;
    }
  }

  function startPlayerTimer(job) {
    clearPlayerTimer();
    state.playerTimer = setTimeout(function () {
      state.playerTimer = null;
      if (!dom.playerWrap.classList.contains('video-ready')) showPlayerFallback(state.job || job);
    }, 8000);
  }

  /** 播放区兜底提示：编码不支持 / 文件不可用，并给出下载原视频入口。 */
  function showPlayerFallback(job) {
    clearPlayerTimer();
    dom.playerWrap.classList.remove('video-ready');
    try { dom.video.pause(); } catch (e) { /* noop */ }
    var mediaUrl = (job && job.media_url) || dom.video.getAttribute('src') || '';
    var title = (job && job.title) || '视频';
    var placeholder = dom.playerPlaceholder;
    placeholder.classList.remove('is-hidden');
    placeholder.classList.add('is-static');

    function render(message, withDownload) {
      var html = '<svg class="icon player-fallback-icon"><use href="#i-alert"/></svg>' +
        '<p>' + escapeHtml(message) + '</p>';
      if (withDownload && mediaUrl) {
        html += '<a class="btn btn-ghost player-download" href="' + escapeHtml(mediaUrl) +
          '" download="' + escapeHtml(safeFilename(title, 'mp4')) + '">' +
          '<svg class="icon"><use href="#i-download"/></svg>下载原视频</a>';
      }
      placeholder.innerHTML = html;
    }

    render('浏览器无法播放该视频编码（如 HEVC / MPEG-4），但并不影响转写、总结与问答。', true);
    if (!mediaUrl) {
      render('没有可用的视频文件（可能已被清理），但不影响转写、总结与问答。', false);
      return;
    }
    // 校验媒体文件是否真实存在：不存在则提示“文件不可用”，避免误判为编码问题
    fetch(mediaUrl, { method: 'GET', headers: { Range: 'bytes=0-1' }, cache: 'no-store' }).then(function (res) {
      if (res.ok || res.status === 206) return;
      render('视频文件不可用（可能已被清理），但不影响转写、总结与问答。', false);
    }, function () {
      render('视频文件不可用（可能已被清理），但不影响转写、总结与问答。', false);
    });
  }

  function onVideoReady() {
    clearPlayerTimer();
    var video = dom.video;
    if (!video.getAttribute('src')) return;
    // 容器可解析但视频画面无法解码（如 MPEG-4 Part 2 / HEVC）：videoWidth 为 0
    if (video.readyState >= 1 && video.videoWidth === 0) {
      showPlayerFallback(state.job);
      return;
    }
    dom.playerWrap.classList.add('video-ready');
    setPlayerPlaceholder('hidden');
  }

  function onVideoError() {
    if (!dom.video.getAttribute('src')) return;
    showPlayerFallback(state.job);
  }

  function renderJobMeta(job) {
    var parts = [];
    function item(icon, text) {
      return '<span class="meta-item"><svg class="icon"><use href="' + icon + '"/></svg><span>' + escapeHtml(text) + '</span></span>';
    }
    parts.push(item('#i-file-video', sourceLabel(job.source_type)));
    if (job.duration) parts.push(item('#i-clock', fmtTime(job.duration)));
    if (job.segment_count) parts.push(item('#i-list', job.segment_count + ' 段转写'));
    if (job.message_count) parts.push(item('#i-message', job.message_count + ' 条对话'));
    if (job.created_at) parts.push(item('#i-clock', fmtDate(job.created_at)));
    if (isActive(job)) parts.push(item('#i-spark', (job.stage_label || '处理中') + ' ' + clampPct(job.progress) + '%'));
    return parts.join('<span class="meta-sep"></span>');
  }

  function renderStageStrip(job) {
    var stages = Array.isArray(job.stages) ? job.stages : [];
    if (!stages.length) {
      dom.stageStrip.innerHTML = '<span class="stage-chip is-running"><span class="dot"></span>' +
        escapeHtml(job.stage_label || '处理中') + '</span><div class="stage-overall"><div class="bar">' +
        '<div class="bar-fill" style="width:' + clampPct(job.progress) + '%"></div></div><span class="pct">' +
        clampPct(job.progress) + '%</span></div>';
      return;
    }
    var chips = stages.map(function (stage) {
      return '<span class="stage-chip is-' + escapeHtml(stage.status || 'pending') + '"><span class="dot"></span>' +
        escapeHtml(stage.label || stage.key || '') + '</span>';
    });
    var pct = clampPct(job.progress);
    dom.stageStrip.innerHTML = chips.join('<span class="stage-arrow">›</span>') +
      '<div class="stage-overall"><div class="bar"><div class="bar-fill" style="width:' + pct +
      '%"></div></div><span class="pct">' + pct + '%</span></div>';
  }

  function renderJobChrome() {
    var job = state.job;
    if (!job) return;
    var title = job.title || '未命名视频';
    dom.jobTitle.textContent = title;
    document.title = title + ' · 视频分析 · DeepSeek';
    dom.jobStatusBadge.className = 'status-badge ' + statusInfo(job).cls;
    dom.jobStatusBadge.textContent = statusInfo(job).label;
    dom.jobSourceBadge.textContent = sourceLabel(job.source_type);
    dom.jobMeta.innerHTML = renderJobMeta(job);

    dom.btnRegenerate.classList.toggle('is-hidden', !(job.status === 'done' && job.has_transcript));
    dom.btnDeleteJob.disabled = false;

    var hasError = job.status === 'error';
    dom.errorBanner.classList.toggle('is-hidden', !hasError);
    if (hasError) dom.errorMessage.textContent = job.error || '未知错误，请查看后端日志。';

    var active = isActive(job);
    dom.stageStrip.classList.toggle('is-hidden', !active);
    if (active) renderStageStrip(job);

    if (job.segment_count) dom.transcriptTabCount.textContent = String(job.segment_count);

    updatePlayer(job);
  }

  function loadJobDetail(jobId, options) {
    var opts = options || {};
    var token = ++state.detailToken;
    return api('/api/jobs/' + encodeURIComponent(jobId)).then(function (data) {
      if (token !== state.detailToken) return;
      if (!data || !data.job) throw new ApiError('后端返回的数据不完整', 0);
      state.job = data.job;
      setView('job');
      renderJobChrome();
      updateActiveListItem();
      ensureActiveTab(true);
      scheduleJobPoll();
    }, function (err) {
      if (token !== state.detailToken) return;
      state.job = null;
      if (err.status === 404) {
        toast('作业不存在或已被删除', 'error');
        clearHash();
        closeJob();
        loadJobs();
        return;
      }
      setView('empty');
      toast('载入作业失败：' + err.message, 'error', 4600);
    });
  }

  function openJob(jobId, options) {
    var opts = options || {};
    if (!jobId) return;
    if (state.currentJobId === jobId && state.job && !opts.force) {
      if (!opts.fromHash) setHashJob(jobId);
      return;
    }
    cancelChatStreaming();
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.currentJobId = jobId;
    state.job = null;
    state.detailToken++;
    resetJobData();
    resetPlayer();
    setActiveTab('summary', { silent: true });
    setView('loading');
    updateActiveListItem();
    if (!opts.fromHash) setHashJob(jobId);
    loadJobDetail(jobId);
  }

  function closeJob() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    cancelChatStreaming();
    state.currentJobId = null;
    state.job = null;
    state.detailToken++;
    document.title = '视频分析 · DeepSeek';
    setView('empty');
    updateActiveListItem();
  }

  /* =======================================================================
     轮询
     ======================================================================= */

  function scheduleJobPoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!state.job) return;
    if (!isActive(state.job)) return;
    state.pollTimer = setTimeout(pollCurrentJob, 1200);
  }

  function pollCurrentJob() {
    state.pollTimer = null;
    var job = state.job;
    if (!job) return;
    var jobId = job.id;
    api('/api/jobs/' + encodeURIComponent(jobId)).then(function (data) {
      if (!state.job || state.job.id !== jobId) return;
      if (!data || !data.job) throw new ApiError('后端返回的数据不完整', 0);
      var next = data.job;
      var prevStatus = state.job.status;
      state.job = next;
      renderJobChrome();
      upsertJobInList(next);

      if (prevStatus !== next.status && (next.status === 'done' || next.status === 'error')) {
        state.data.transcript = null;
        state.data.ocr = null;
        ensureActiveTab(true);
        if (next.status === 'done') toast('分析完成', 'success');
        else toast('分析失败：' + (next.error || '未知错误'), 'error', 6000);
        loadJobs();
      } else if (isActive(next)) {
        if (state.activeTab === 'summary') renderSummaryPanel(false);
        else if (state.activeTab === 'transcript' && !next.has_transcript) {
          dom.tabPanels.transcript.innerHTML = waitingPanelHtml(next, '正在转写音频', '转写完成后会在这里显示带时间戳的文本。');
        } else if (state.activeTab === 'ocr' && !next.has_ocr) {
          dom.tabPanels.ocr.innerHTML = waitingPanelHtml(next, '正在识别画面文字', '抽取关键帧并运行本地 OCR 后，会在这里显示时间线与缩略图。');
        } else if (state.activeTab === 'chat') {
          updateComposer();
        }
        scheduleJobPoll();
      }
    }, function (err) {
      if (!state.job || state.job.id !== jobId) return;
      if (err.status === 404) {
        toast('作业已被删除', 'error');
        clearHash();
        closeJob();
        loadJobs();
        return;
      }
      if (isActive(state.job)) {
        state.pollTimer = setTimeout(pollCurrentJob, 2500);
      }
    });
  }

  /* =======================================================================
     播放器 / 跳转
     ======================================================================= */

  function seekTo(seconds) {
    var video = dom.video;
    if (!video || !video.getAttribute('src')) {
      toast('视频还未准备好，暂时无法跳转', 'info', 2400);
      return;
    }
    var target = Math.max(0, Number(seconds) || 0);
    try {
      if (video.readyState >= 1 && isFinite(video.duration) && video.duration > 0) {
        video.currentTime = Math.min(target, Math.max(0, video.duration - 0.05));
      } else {
        video.currentTime = target;
        state.pendingSeek = target;
      }
    } catch (e) {
      state.pendingSeek = target;
    }
    if (video.paused) {
      var promise = video.play();
      if (promise && promise.catch) promise.catch(function () { /* 浏览器可能阻止自动播放 */ });
    }
    flashPlayer();
  }

  function flashPlayer() {
    var flash = document.createElement('div');
    flash.className = 'player-flash';
    dom.playerWrap.appendChild(flash);
    setTimeout(function () { flash.remove(); }, 620);
  }

  function highlightTranscriptSegment(segmentId) {
    var list = $('#transcriptList');
    if (!list) return;
    if (!canFindRenderedSegment(segmentId)) {
      renderTranscriptUntil(segmentId);
    }
    var row = list.querySelector('.tx-row[data-segment-id="' + String(segmentId) + '"]');
    if (!row) return;
    $$('.tx-row.is-active', list).forEach(function (item) { item.classList.remove('is-active'); });
    row.classList.add('is-active');
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    state.currentSegmentId = segmentId;
  }

  function canFindRenderedSegment(segmentId) {
    var list = $('#transcriptList');
    return !!(list && list.querySelector('.tx-row[data-segment-id="' + String(segmentId) + '"]'));
  }

  function renderTranscriptUntil(segmentId) {
    var filtered = state.transcriptFiltered || [];
    var index = -1;
    for (var i = 0; i < filtered.length; i++) {
      if (filtered[i].id === segmentId) { index = i; break; }
    }
    if (index === -1) return;
    var guard = 0;
    while (state.transcriptShown <= index && guard < 50) {
      renderMoreTranscript();
      guard++;
    }
  }

  function findSegmentAt(time) {
    var tx = state.data.transcript && state.data.transcript.data;
    if (!tx || !Array.isArray(tx.segments) || !tx.segments.length) return null;
    var segments = tx.segments;
    var low = 0;
    var high = segments.length - 1;
    var found = null;
    while (low <= high) {
      var mid = (low + high) >> 1;
      var seg = segments[mid];
      if (time < seg.start) high = mid - 1;
      else if (time >= seg.end) low = mid + 1;
      else { found = seg; break; }
    }
    return found;
  }

  function syncActiveSegmentByTime() {
    if (state.activeTab !== 'transcript') return;
    if (!state.data.transcript || !state.data.transcript.data) return;
    var seg = findSegmentAt(dom.video.currentTime || 0);
    if (!seg || seg.id === state.currentSegmentId) return;
    state.currentSegmentId = seg.id;
    var list = $('#transcriptList');
    if (!list) return;
    var row = list.querySelector('.tx-row[data-segment-id="' + String(seg.id) + '"]');
    if (!row) return;
    $$('.tx-row.is-active', list).forEach(function (item) { item.classList.remove('is-active'); });
    row.classList.add('is-active');
  }

  var onTimeUpdate = debounce(syncActiveSegmentByTime, 300);

  /* =======================================================================
     标签页
     ======================================================================= */

  function setActiveTab(tab, options) {
    var opts = options || {};
    state.activeTab = tab;
    $$('.tab').forEach(function (btn) {
      var active = btn.dataset.tab === tab;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    });
    Object.keys(dom.tabPanels).forEach(function (key) {
      dom.tabPanels[key].classList.toggle('is-hidden', key !== tab);
    });
    if (state.job && !opts.silent) {
      var promise = ensureActiveTab(false);
      if (tab === 'chat' && promise && typeof promise.then === 'function') {
        promise.then(scrollChatPanelIntoView);
      }
    }
  }

  function scrollChatPanelIntoView() {
    if (!dom.content) return;
    requestAnimationFrame(function () {
      dom.content.scrollTop = dom.content.scrollHeight;
    });
  }

  function ensureActiveTab(force) {
    var job = state.job;
    if (!job) return Promise.resolve();
    if (state.activeTab === 'summary') {
      renderSummaryPanel(!!force);
      return Promise.resolve();
    }
    if (state.activeTab === 'transcript') return ensureTranscript(force);
    if (state.activeTab === 'ocr') return ensureOcr(force);
    if (state.activeTab === 'chat') return ensureChat(force);
    return Promise.resolve();
  }

  function panelLoadingHtml(text) {
    return '<div class="panel-loading"><div class="spinner"></div><p>' + escapeHtml(text || '正在载入…') + '</p></div>';
  }

  function panelEmptyHtml(icon, title, desc, extra) {
    return '<div class="panel-empty">' +
      '<div class="empty-icon"><svg class="icon"><use href="' + icon + '"/></svg></div>' +
      '<h3>' + escapeHtml(title) + '</h3>' +
      '<p>' + desc + '</p>' +
      (extra || '') + '</div>';
  }

  function panelErrorHtml(job) {
    return '<div class="panel-empty">' +
      '<div class="empty-icon"><svg class="icon"><use href="#i-alert"/></svg></div>' +
      '<h3>作业处理失败</h3>' +
      '<p>' + escapeHtml(job.error || '未知错误，请查看后端日志。') + '</p>' +
      '<button class="btn btn-primary" type="button" data-action="retry-job">重试</button></div>';
  }

  function waitingPanelHtml(job, title, desc) {
    var pct = clampPct(job.progress);
    return '<div class="panel-empty">' +
      '<div class="empty-icon"><div class="spinner" style="width:20px;height:20px"></div></div>' +
      '<h3>' + escapeHtml(title) + '</h3>' +
      '<p>' + escapeHtml(desc || '') + '<br>当前阶段：' + escapeHtml(job.stage_label || '处理中') + ' · ' + pct + '%</p>' +
      '</div>';
  }

  /* ---------------------------- 总结 ---------------------------- */

  function renderSummaryPanel(force) {
    var job = state.job;
    if (!job) return;
    var root = dom.tabPanels.summary;

    if (job.status === 'error') {
      root.innerHTML = panelErrorHtml(job);
      return;
    }
    if (!job.has_summary) {
      if (isActive(job)) {
        root.innerHTML = waitingPanelHtml(job, '正在生成总结', '转写与画面识别完成后会自动生成结构化总结。');
      } else {
        var extra = job.has_transcript
          ? '<button class="btn btn-primary" type="button" data-action="regenerate">重新生成总结</button>'
          : '';
        root.innerHTML = panelEmptyHtml('#i-spark', '暂无总结',
          '本次作业没有生成可用的总结内容。' + (job.has_transcript ? '可以尝试重新生成。' : ''), extra);
      }
      return;
    }

    var summary = job.summary || {};
    var degraded = !summary.generated_by;
    var chapters = Array.isArray(summary.chapters) ? summary.chapters : [];
    var keyPoints = Array.isArray(summary.key_points) ? summary.key_points : [];
    var keywords = Array.isArray(summary.keywords) ? summary.keywords : [];
    var quotes = Array.isArray(summary.quotes) ? summary.quotes : [];
    var visualNotes = Array.isArray(summary.visual_notes) ? summary.visual_notes : [];

    var parts = [];

    if (degraded) {
      var noKeyText;
      if (state.settings && !state.settings.deepseek_api_key_set) {
        noKeyText = '<strong>尚未配置 DeepSeek API Key</strong>当前显示的是转写降级摘要。在「设置」中填写 Key 后可生成结构化总结与视频问答。';
      } else {
        noKeyText = '<strong>本次为降级摘要</strong>未成功调用 DeepSeek，当前只展示转写节选。请检查 API Key 与网络后重新生成。';
      }
      parts.push('<div class="no-key-banner">' +
        '<svg class="icon"><use href="#i-key"/></svg>' +
        '<div class="no-key-text">' + noKeyText + '</div>' +
        '<button class="btn btn-primary" type="button" data-action="open-settings">去设置</button>' +
        '</div>');
    }

    parts.push('<div class="summary-hero">' +
      '<div class="summary-hero-label"><svg class="icon"><use href="#i-spark"/></svg>一句话结论</div>' +
      '<p class="summary-one-liner">' + escapeHtml(summary.one_liner || '（暂无一句话结论）') + '</p>' +
      '<div class="summary-hero-meta">' +
      (degraded
        ? '<span class="meta-item"><svg class="icon"><use href="#i-alert"/></svg><span>降级摘要（未调用大模型）</span></span>'
        : '<span class="meta-item"><svg class="icon"><use href="#i-spark"/></svg><span>由 ' + escapeHtml(summary.generated_by) + ' 生成</span></span>') +
      (summary.created_at ? '<span class="meta-item"><svg class="icon"><use href="#i-clock"/></svg><span>' +
        escapeHtml(fmtDate(summary.created_at)) + '</span></span>' : '') +
      (chapters.length ? '<span class="meta-item"><svg class="icon"><use href="#i-book"/></svg><span>' +
        chapters.length + ' 个章节</span></span>' : '') +
      '</div></div>');

    var left = [];
    if (summary.overview) {
      left.push('<section class="card overview-card">' +
        '<div class="card-head"><h3><svg class="icon"><use href="#i-book"/></svg>内容概览</h3></div>' +
        '<div class="md-body">' + Markdown.render(summary.overview) + '</div></section>');
    }
    if (chapters.length) {
      var chapterHtml = chapters.map(function (chapter) {
        return '<button class="chapter-card" type="button" data-seek="' + Number(chapter.start || 0) + '">' +
          '<span class="chapter-time">' + fmtTime(chapter.start) + ' – ' + fmtTime(chapter.end) + '</span>' +
          '<span class="chapter-body"><span class="chapter-title">' + escapeHtml(chapter.title || '') + '</span>' +
          '<span class="chapter-summary">' + escapeHtml(chapter.summary || '') + '</span></span></button>';
      }).join('');
      left.push('<section class="card"><div class="card-head"><h3><svg class="icon"><use href="#i-list"/></svg>章节</h3>' +
        '<span class="card-extra">点击跳转播放</span></div><div class="chapter-list">' + chapterHtml + '</div></section>');
    }

    var right = [];
    if (keyPoints.length) {
      right.push('<section class="card"><div class="card-head"><h3><svg class="icon"><use href="#i-check"/></svg>要点</h3></div>' +
        '<ul class="simple-list key-points">' + keyPoints.map(function (point) {
          return '<li>' + escapeHtml(point) + '</li>';
        }).join('') + '</ul></section>');
    }
    if (keywords.length) {
      right.push('<section class="card"><div class="card-head"><h3><svg class="icon"><use href="#i-tag"/></svg>关键词</h3>' +
        '<span class="card-extra">点击在转写中搜索</span></div><div class="chip-row">' +
        keywords.map(function (keyword) {
          return '<button class="chip" type="button" data-keyword="' + escapeHtml(keyword) + '" title="在转写中搜索「' +
            escapeHtml(keyword) + '」">' + escapeHtml(keyword) + '</button>';
        }).join('') + '</div></section>');
    }
    if (quotes.length) {
      right.push('<section class="card"><div class="card-head"><h3><svg class="icon"><use href="#i-quote"/></svg>金句</h3></div>' +
        '<div class="quote-list">' + quotes.map(function (quote) {
          return '<div class="quote-item">' + Markdown.renderInline(quote) + '</div>';
        }).join('') + '</div></section>');
    }
    if (visualNotes.length) {
      right.push('<section class="card"><div class="card-head"><h3><svg class="icon"><use href="#i-image"/></svg>画面文字线索</h3></div>' +
        '<ul class="visual-note-list">' + visualNotes.map(function (note) {
          return '<li><svg class="icon"><use href="#i-scan"/></svg><span>' + escapeHtml(note) + '</span></li>';
        }).join('') + '</ul></section>');
    }

    if (!left.length && !right.length) {
      parts.push(panelEmptyHtml('#i-spark', '暂无总结内容', '总结数据为空，可以尝试重新生成。',
        job.has_transcript ? '<button class="btn btn-primary" type="button" data-action="regenerate">重新生成总结</button>' : ''));
    } else {
      parts.push('<div class="summary-columns"><div class="summary-col">' + left.join('') + '</div>' +
        '<div class="summary-col">' + right.join('') + '</div></div>');
    }

    if (summary.generated_by || summary.created_at) {
      parts.push('<div class="summary-footer"><span>生成模型：' + escapeHtml(summary.generated_by || '——') +
        '</span><span>生成时间：' + escapeHtml(fmtDate(summary.created_at) || '——') + '</span></div>');
    }

    root.innerHTML = parts.join('');
    if (force) root.classList.remove('is-hidden');
  }

  /* ---------------------------- 转写 ---------------------------- */

  function ensureTranscript(force) {
    var job = state.job;
    if (!job) return Promise.resolve();
    var root = dom.tabPanels.transcript;
    var cache = state.data.transcript;
    if (!force && cache && cache.status === 'ready') return Promise.resolve();
    if (!force && cache && cache.status === 'loading') return cache.promise;

    if (!job.has_transcript) {
      if (isActive(job)) {
        root.innerHTML = waitingPanelHtml(job, '正在转写音频', '转写完成后会在这里显示带时间戳的文本。');
      } else if (job.status === 'error') {
        root.innerHTML = panelErrorHtml(job);
      } else {
        root.innerHTML = panelEmptyHtml('#i-list', '暂无转写',
          '本次作业没有生成转写文本。可能是音频为空或转写未完成。');
      }
      return Promise.resolve();
    }

    root.innerHTML = panelLoadingHtml('正在载入转写…');
    var promise = api('/api/jobs/' + encodeURIComponent(job.id) + '/transcript').then(function (data) {
      var transcript = (data && data.transcript) || { segments: [], text: '' };
      state.data.transcript = { status: 'ready', data: transcript };
      if (Array.isArray(transcript.segments)) {
        dom.transcriptTabCount.textContent = transcript.segments.length ? String(transcript.segments.length) : '';
      }
      renderTranscriptPanel();
    }, function (err) {
      state.data.transcript = { status: 'error', error: err };
      if (err.status === 404) {
        root.innerHTML = panelEmptyHtml('#i-list', '转写尚未完成', '转写仍在进行中，完成后会自动刷新。');
      } else {
        root.innerHTML = panelEmptyHtml('#i-alert', '转写载入失败', escapeHtml(err.message));
      }
    });
    state.data.transcript = { status: 'loading', promise: promise };
    return promise;
  }

  function applyTranscriptFilter() {
    var tx = state.data.transcript && state.data.transcript.data;
    if (!tx) return;
    var segments = Array.isArray(tx.segments) ? tx.segments : [];
    var query = state.transcriptFilter.trim().toLowerCase();
    state.transcriptFiltered = query
      ? segments.filter(function (seg) { return String(seg.text || '').toLowerCase().indexOf(query) !== -1; })
      : segments.slice();
    state.transcriptShown = 0;
    var list = $('#transcriptList');
    if (list) list.innerHTML = '';
    var clearBtn = $('#btnClearSearch');
    if (clearBtn) clearBtn.classList.toggle('is-hidden', !state.transcriptFilter);
    updateTranscriptStats();
    renderMoreTranscript();
  }

  function updateTranscriptStats() {
    var stats = $('#transcriptStats');
    if (!stats) return;
    var tx = state.data.transcript && state.data.transcript.data;
    var total = (tx && Array.isArray(tx.segments)) ? tx.segments.length : 0;
    if (state.transcriptFilter.trim()) {
      stats.innerHTML = '匹配 <strong>' + state.transcriptFiltered.length + '</strong> / ' + total + ' 段';
    } else {
      stats.innerHTML = '共 <strong>' + total + '</strong> 段';
    }
  }

  function bindTranscriptObserver() {
    if (state.transcriptObserver) state.transcriptObserver.disconnect();
    var sentinel = $('#transcriptMore');
    if (!sentinel || !('IntersectionObserver' in window)) {
      if (sentinel) sentinel.classList.add('is-hidden');
      return;
    }
    state.transcriptObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) renderMoreTranscript();
      });
    }, { root: null, rootMargin: '300px 0px' });
    state.transcriptObserver.observe(sentinel);
  }

  function renderMoreTranscript() {
    var list = $('#transcriptList');
    if (!list) return;
    var filtered = state.transcriptFiltered || [];
    var total = filtered.length;
    var from = state.transcriptShown;
    var batchSize = 180;
    if (from >= total) {
      var moreRow = $('#transcriptMore');
      if (moreRow) moreRow.classList.add('is-hidden');
      return;
    }
    var to = Math.min(from + batchSize, total);
    var query = state.transcriptFilter.trim();
    var frag = document.createDocumentFragment();
    for (var i = from; i < to; i++) {
      frag.appendChild(buildTranscriptRow(filtered[i], query));
    }
    list.appendChild(frag);
    state.transcriptShown = to;
    var row = $('#transcriptMore');
    if (row) {
      if (to < total) {
        row.classList.remove('is-hidden');
        row.textContent = '已显示 ' + to + ' / ' + total + ' 段，滚动加载更多…';
        bindTranscriptObserver();
      } else {
        row.classList.add('is-hidden');
        if (state.transcriptObserver) state.transcriptObserver.disconnect();
      }
    }
  }

  function buildTranscriptRow(seg, query) {
    var row = document.createElement('div');
    row.className = 'tx-row';
    row.dataset.segmentId = String(seg.id);
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'ts';
    button.dataset.seek = String(seg.start);
    button.textContent = fmtTime(seg.start);
    var text = document.createElement('div');
    text.className = 'tx-text';
    if (query) text.innerHTML = highlightText(seg.text || '', query);
    else text.textContent = seg.text || '';
    row.appendChild(button);
    row.appendChild(text);
    return row;
  }

  function highlightText(source, query) {
    var content = String(source || '');
    var needle = String(query || '').trim();
    if (!needle) return escapeHtml(content);
    var lower = content.toLowerCase();
    var lowerNeedle = needle.toLowerCase();
    var html = '';
    var pos = 0;
    var index;
    while ((index = lower.indexOf(lowerNeedle, pos)) !== -1) {
      html += escapeHtml(content.slice(pos, index));
      html += '<mark>' + escapeHtml(content.slice(index, index + needle.length)) + '</mark>';
      pos = index + needle.length;
    }
    html += escapeHtml(content.slice(pos));
    return html || escapeHtml(content);
  }

  function renderTranscriptPanel() {
    var tx = state.data.transcript && state.data.transcript.data;
    if (!tx) return;
    var root = dom.tabPanels.transcript;
    root.innerHTML =
      '<div class="transcript-toolbar">' +
      '<div class="search-box">' +
      '<svg class="icon"><use href="#i-search"/></svg>' +
      '<input id="transcriptSearch" class="input" type="search" placeholder="搜索转写内容…" autocomplete="off" spellcheck="false" value="' +
      escapeHtml(state.transcriptFilter) + '">' +
      '<button id="btnClearSearch" class="icon-btn clear-search' + (state.transcriptFilter ? '' : ' is-hidden') +
      '" type="button" title="清除搜索" aria-label="清除搜索"><svg class="icon"><use href="#i-close"/></svg></button>' +
      '</div>' +
      '<span class="transcript-stats" id="transcriptStats"></span>' +
      '<div class="toolbar-right">' +
      '<button id="btnCopyTranscript" class="btn btn-ghost" type="button"><svg class="icon"><use href="#i-copy"/></svg>复制全文</button>' +
      '<div class="dropdown"><button id="btnExportTranscript" class="btn btn-ghost" type="button">' +
      '<svg class="icon"><use href="#i-download"/></svg>导出<svg class="icon icon-sm"><use href="#i-chevron"/></svg></button>' +
      '<div class="dropdown-menu is-hidden" id="exportMenu">' +
      '<button type="button" data-export="txt">导出 TXT（带时间戳）</button>' +
      '<button type="button" data-export="srt">导出 SRT 字幕</button>' +
      '</div></div></div></div>' +
      '<div class="transcript-list" id="transcriptList"></div>' +
      '<div class="load-more-row is-hidden" id="transcriptMore"></div>';

    var search = $('#transcriptSearch');
    var clear = $('#btnClearSearch');
    var copyBtn = $('#btnCopyTranscript');
    var exportBtn = $('#btnExportTranscript');
    var exportMenu = $('#exportMenu');

    search.addEventListener('input', debounce(function () {
      state.transcriptFilter = search.value;
      applyTranscriptFilter();
    }, 180));
    clear.addEventListener('click', function () {
      state.transcriptFilter = '';
      search.value = '';
      applyTranscriptFilter();
      search.focus();
    });
    copyBtn.addEventListener('click', copyTranscript);
    exportBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      state.exportOpen = exportMenu.classList.contains('is-hidden');
      exportMenu.classList.toggle('is-hidden', !state.exportOpen);
    });
    $$('[data-export]', exportMenu).forEach(function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        exportMenu.classList.add('is-hidden');
        state.exportOpen = false;
        exportTranscript(btn.dataset.export);
      });
    });

    applyTranscriptFilter();
  }

  function transcriptSegments() {
    var tx = state.data.transcript && state.data.transcript.data;
    return (tx && Array.isArray(tx.segments)) ? tx.segments : [];
  }

  function transcriptFullText() {
    var tx = state.data.transcript && state.data.transcript.data;
    var segments = transcriptSegments();
    if (!segments.length) return (tx && tx.text) || '';
    return segments.map(function (seg) { return '[' + fmtTime(seg.start) + '] ' + (seg.text || ''); }).join('\n');
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function fallbackCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    area.remove();
    return ok;
  }

  function copyTranscript() {
    var text = transcriptFullText();
    if (!text) {
      toast('没有可复制的转写内容', 'info');
      return;
    }
    Promise.resolve(copyText(text)).then(function (ok) {
      if (ok === false) toast('复制失败，请手动选择文本复制', 'error');
      else toast('已复制转写全文', 'success', 2400);
    });
  }

  function exportTranscript(format) {
    var segments = transcriptSegments();
    if (!segments.length) {
      toast('没有可导出的转写内容', 'info');
      return;
    }
    var title = (state.job && state.job.title) || '视频分析';
    var content;
    var ext = format === 'srt' ? 'srt' : 'txt';
    if (format === 'srt') {
      content = segments.map(function (seg, index) {
        return (index + 1) + '\n' + fmtSrtTime(seg.start) + ' --> ' + fmtSrtTime(seg.end) + '\n' + (seg.text || '') + '\n';
      }).join('\n');
    } else {
      content = transcriptFullText();
    }
    var blob = new Blob(['\ufeff', content], { type: 'text/plain;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = safeFilename(title, ext);
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1200);
    toast('已导出 ' + ext.toUpperCase() + ' 文件', 'success', 2600);
  }

  function jumpToRow(row) {
    var segmentId = Number(row.dataset.segmentId);
    var filtered = state.transcriptFiltered || [];
    var target = null;
    for (var i = 0; i < filtered.length; i++) {
      if (filtered[i].id === segmentId) { target = filtered[i]; break; }
    }
    if (!target) {
      var segments = transcriptSegments();
      for (var j = 0; j < segments.length; j++) {
        if (segments[j].id === segmentId) { target = segments[j]; break; }
      }
    }
    if (!target) return;
    seekTo(target.start);
    highlightTranscriptSegment(segmentId);
  }

  /* ---------------------------- 画面文字 ---------------------------- */

  function ensureOcr(force) {
    var job = state.job;
    if (!job) return Promise.resolve();
    var root = dom.tabPanels.ocr;
    var cache = state.data.ocr;
    if (!force && cache && cache.status === 'ready') return Promise.resolve();
    if (!force && cache && cache.status === 'loading') return cache.promise;

    if (!job.has_ocr) {
      if (isActive(job)) {
        root.innerHTML = waitingPanelHtml(job, '正在识别画面文字', '抽取关键帧并运行本地 OCR 后，会在这里显示时间线与缩略图。');
      } else if (job.status === 'error') {
        root.innerHTML = panelErrorHtml(job);
      } else {
        root.innerHTML = panelEmptyHtml('#i-scan', '暂无画面文字',
          '本次作业未识别到画面文字。可能是纯口语视频，或 OCR 阶段被跳过。');
      }
      return Promise.resolve();
    }

    root.innerHTML = panelLoadingHtml('正在载入画面文字…');
    var promise = api('/api/jobs/' + encodeURIComponent(job.id) + '/ocr').then(function (data) {
      var ocr = (data && data.ocr) || { frames: [], text: '' };
      state.data.ocr = { status: 'ready', data: ocr };
      var frames = Array.isArray(ocr.frames) ? ocr.frames : [];
      dom.ocrTabCount.textContent = frames.length ? String(frames.length) : '';
      renderOcrPanel();
    }, function (err) {
      state.data.ocr = { status: 'error', error: err };
      if (err.status === 404) {
        root.innerHTML = panelEmptyHtml('#i-scan', '画面文字尚未生成', '作业仍在处理中，完成后会自动刷新。');
      } else {
        root.innerHTML = panelEmptyHtml('#i-alert', '画面文字载入失败', escapeHtml(err.message));
      }
    });
    state.data.ocr = { status: 'loading', promise: promise };
    return promise;
  }

  function frameUrl(jobId, frameName) {
    var name = String(frameName || '').replace(/^frames\//, '');
    if (!name) return '';
    return '/api/jobs/' + encodeURIComponent(jobId) + '/frames/' +
      name.split('/').map(encodeURIComponent).join('/');
  }

  function renderOcrPanel() {
    var job = state.job;
    var ocr = state.data.ocr && state.data.ocr.data;
    if (!job || !ocr) return;
    var root = dom.tabPanels.ocr;
    var frames = Array.isArray(ocr.frames) ? ocr.frames : [];
    if (!frames.length) {
      root.innerHTML = panelEmptyHtml('#i-scan', '未识别到画面文字',
        '关键帧中没有检测到文字。若视频是纯口语内容，这属于正常情况。');
      return;
    }
    var html = '<div class="ocr-toolbar"><span class="transcript-stats">共 <strong>' + frames.length +
      '</strong> 帧</span><span class="muted" style="font-size:12px">点击缩略图放大，点击时间跳转播放</span></div>' +
      '<div class="ocr-timeline">';
    frames.forEach(function (frame, index) {
      var name = String(frame.frame || '').replace(/^frames\//, '');
      var url = name ? frameUrl(job.id, name) : '';
      html += '<div class="ocr-item">' +
        '<div class="ocr-media" data-frame-index="' + index + '" title="点击放大">' +
        (url
          ? '<img loading="lazy" src="' + escapeHtml(url) + '" alt="' + escapeHtml(fmtTime(frame.time)) + ' 画面帧">'
          : '<div class="no-frame"><svg class="icon"><use href="#i-image"/></svg></div>') +
        '<span class="zoom-hint"><svg class="icon"><use href="#i-search"/></svg></span>' +
        '</div>' +
        '<div class="ocr-body"><div class="ocr-body-head">' +
        '<button class="ts" type="button" data-seek="' + Number(frame.time || 0) + '">' + fmtTime(frame.time) + '</button>' +
        (frame.score ? '<span class="ocr-score">置信度 ' + Math.round(frame.score * 100) + '%</span>' : '') +
        '</div><p class="ocr-text' + (frame.text ? '' : ' is-empty') + '">' +
        escapeHtml(frame.text || '（该画面没有识别到文字）') + '</p></div></div>';
    });
    html += '</div>';
    root.innerHTML = html;

    $$('.ocr-media', root).forEach(function (media) {
      var index = Number(media.dataset.frameIndex);
      var frame = frames[index];
      if (!frame) return;
      var img = media.querySelector('img');
      if (img) {
        img.addEventListener('error', function () {
          var holder = document.createElement('div');
          holder.className = 'no-frame';
          holder.innerHTML = '<svg class="icon"><use href="#i-image"/></svg>';
          img.remove();
          media.insertBefore(holder, media.firstChild);
        });
      }
      media.addEventListener('click', function () {
        var url = img ? img.src : '';
        if (!url) return;
        openLightbox(frame, url);
      });
    });
  }

  /* ---------------------------- 对话 ---------------------------- */

  function ensureChat(force) {
    var job = state.job;
    if (!job || !state.chat) return Promise.resolve();
    if (!canChat(job)) {
      renderChatIdle(job);
      updateComposer();
      return Promise.resolve();
    }
    if (!force && state.chat.loaded) {
      updateComposer();
      return Promise.resolve();
    }
    dom.chatLog.innerHTML = panelLoadingHtml('正在载入对话记录…');
    var promise = api('/api/jobs/' + encodeURIComponent(job.id) + '/messages').then(function (data) {
      var messages = (data && Array.isArray(data.messages)) ? data.messages : [];
      state.chat.messages = messages.map(normalizeMessage);
      state.chat.loaded = true;
      renderChatMessages();
      renderChatSuggestions();
      updateComposer();
    }, function (err) {
      state.chat.loaded = true;
      state.chat.messages = [];
      dom.chatLog.innerHTML = '';
      var hint = document.createElement('div');
      hint.className = 'chat-empty';
      hint.innerHTML = '<div class="chat-empty-icon"><svg class="icon"><use href="#i-alert"/></svg></div>' +
        '<h3>对话记录载入失败</h3><p></p>';
      hint.querySelector('p').textContent = err.message;
      dom.chatLog.appendChild(hint);
      renderChatSuggestions();
      updateComposer();
    });
    return promise;
  }

  function normalizeMessage(raw) {
    return {
      id: raw && raw.id != null ? raw.id : 0,
      role: raw && raw.role === 'assistant' ? 'assistant' : 'user',
      content: (raw && raw.content) || '',
      citations: (raw && Array.isArray(raw.citations)) ? raw.citations : [],
      created_at: (raw && raw.created_at) || ''
    };
  }

  function renderChatIdle(job) {
    if (!dom.chatLog) return;
    var title = '和这个视频对话';
    var desc = '基于视频转写内容回答，答案会标注时间戳引用。';
    if (job.status === 'error') {
      title = '作业处理失败';
      desc = '修复问题并重试作业后即可对话。';
    } else if (isActive(job)) {
      title = '转写完成后即可对话';
      desc = '当前阶段：' + (job.stage_label || '处理中') + ' ' + clampPct(job.progress) + '%';
    } else if (!job.has_transcript) {
      title = '没有可用的转写内容';
      desc = '本次作业没有生成转写，无法基于内容回答。';
    }
    dom.chatLog.innerHTML = '<div class="chat-empty">' +
      '<div class="chat-empty-icon"><svg class="icon"><use href="#i-message"/></svg></div>' +
      '<h3>' + escapeHtml(title) + '</h3><p>' + escapeHtml(desc) + '</p></div>';
    dom.chatSuggest.innerHTML = '';
  }

  function renderChatMessages() {
    if (!state.chat) return;
    dom.chatLog.innerHTML = '';
    if (!state.chat.messages.length) {
      dom.chatLog.innerHTML = '<div class="chat-empty">' +
        '<div class="chat-empty-icon"><svg class="icon"><use href="#i-message"/></svg></div>' +
        '<h3>和这个视频对话</h3>' +
        '<p>基于转写内容回答，答案会标注「MM:SS」时间戳引用，点击可跳转播放。试试下面推荐的问题，或直接输入。</p></div>';
      return;
    }
    state.chat.messages.forEach(function (message) { appendMessageNode(message); });
    scrollChat(true);
  }

  function buildSuggestions(job) {
    var summary = (job && job.summary) || {};
    var topics = Array.isArray(summary.topics) ? summary.topics.filter(Boolean) : [];
    var suggestions = [];
    if (topics.length) {
      suggestions.push('这个视频主要讲了什么？');
      topics.slice(0, 4).forEach(function (topic) {
        suggestions.push('关于「' + topic + '」，视频里是怎么说的？');
      });
    } else {
      suggestions.push('这个视频主要讲了什么？', '用三句话总结视频要点', '视频里有哪些关键结论或数字？');
    }
    return suggestions.slice(0, 5);
  }

  function renderChatSuggestions() {
    if (!dom.chatSuggest || !state.chat) return;
    var job = state.job;
    dom.chatSuggest.innerHTML = '';
    if (!job || !canChat(job) || state.chat.streaming) return;
    if (state.chat.messages.length) return;
    state.chat.suggestions = buildSuggestions(job);
    state.chat.suggestions.forEach(function (question) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'suggest-chip';
      chip.textContent = question;
      chip.addEventListener('click', function () { sendChatMessage(question); });
      dom.chatSuggest.appendChild(chip);
    });
  }

  function createMessageNode(message) {
    var wrap = document.createElement('div');
    wrap.className = 'msg msg-' + (message.role === 'user' ? 'user' : 'assistant');
    var avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    if (message.role === 'user') avatar.textContent = '你';
    else avatar.innerHTML = '<svg class="icon"><use href="#i-spark"/></svg>';
    var main = document.createElement('div');
    main.className = 'msg-main';
    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    var content = document.createElement('div');
    content.className = message.role === 'user' ? 'msg-user-text' : 'md-body';
    if (message.role === 'user') content.textContent = message.content || '';
    else content.innerHTML = Markdown.render(message.content || '');
    bubble.appendChild(content);
    var citations = document.createElement('div');
    citations.className = 'msg-citations';
    main.appendChild(bubble);
    main.appendChild(citations);
    var meta = document.createElement('div');
    meta.className = 'msg-meta';
    main.appendChild(meta);
    wrap.appendChild(avatar);
    wrap.appendChild(main);
    if (message.citations && message.citations.length) renderCitationChips(citations, message.citations);
    return { el: wrap, bubble: bubble, content: content, citations: citations, meta: meta };
  }

  function appendMessageNode(message) {
    var node = createMessageNode(message);
    var empty = dom.chatLog.querySelector('.chat-empty');
    if (empty) empty.remove();
    dom.chatLog.appendChild(node.el);
    return node;
  }

  function renderCitationChips(container, citations) {
    if (!container) return;
    container.innerHTML = '';
    (citations || []).forEach(function (citation) {
      if (!citation) return;
      var start = Number(citation.start) || 0;
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'citation-chip';
      chip.dataset.seek = String(start);
      if (citation.segment_id != null) chip.dataset.segmentId = String(citation.segment_id);
      if (citation.text) chip.title = String(citation.text);
      chip.innerHTML = '<svg class="icon"><use href="#i-clock"/></svg><span></span>';
      chip.querySelector('span').textContent = '【' + fmtTime(start) + '】';
      container.appendChild(chip);
    });
  }

  function scrollChat(force) {
    var log = dom.chatLog;
    if (!log) return;
    var nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 140;
    if (force || nearBottom) log.scrollTop = log.scrollHeight;
  }

  function createStreamRenderer(contentEl, assistant) {
    var lastPaint = 0;
    var timer = null;
    var finished = false;
    function paint(isFinal) {
      var html = Markdown.render(assistant.content || '');
      if (!html) html = '<span class="muted">…</span>';
      if (!isFinal) html += '<span class="typing-caret"></span>';
      contentEl.innerHTML = html;
      if (!isFinal) scrollChat(false);
    }
    function tick() {
      var now = performance.now();
      if (now - lastPaint > 80) {
        lastPaint = now;
        paint(false);
      } else if (!timer) {
        timer = setTimeout(function () {
          timer = null;
          lastPaint = performance.now();
          if (!finished) paint(false);
        }, 80);
      }
    }
    function finish() {
      finished = true;
      if (timer) clearTimeout(timer);
      paint(true);
    }
    return { tick: tick, finish: finish };
  }

  function consumeSse(body, onEvent) {
    var reader = body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buffer = '';
    function processBlock(block) {
      var event = 'message';
      var dataLines = [];
      block.split('\n').forEach(function (line) {
        if (line.indexOf('event:') === 0) event = line.slice(6).trim();
        else if (line.indexOf('data:') === 0) dataLines.push(line.slice(5).replace(/^ /, ''));
      });
      if (!dataLines.length) return;
      var raw = dataLines.join('\n');
      var data = null;
      try { data = JSON.parse(raw); } catch (e) { data = { text: raw }; }
      onEvent(event, data);
    }
    function pump() {
      return reader.read().then(function (result) {
        if (result.done) {
          buffer += decoder.decode();
          if (buffer.trim()) processBlock(buffer.trim());
          return;
        }
        buffer += decoder.decode(result.value, { stream: true });
        buffer = buffer.replace(/\r\n/g, '\n');
        var index;
        while ((index = buffer.indexOf('\n\n')) !== -1) {
          var block = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          if (block.trim()) processBlock(block);
        }
        return pump();
      });
    }
    return pump();
  }

  function updateComposer() {
    var job = state.job;
    var chat = state.chat;
    if (!dom.chatInput || !dom.btnSend || !chat) return;
    var ready = canChat(job);
    var streaming = chat.streaming;
    dom.chatInput.disabled = !ready || streaming;
    var hasText = !!dom.chatInput.value.trim();
    dom.btnSend.disabled = !ready || streaming || !hasText;
    if (streaming) {
      dom.btnSend.innerHTML = '<span class="spinner spinner-sm"></span><span>回答中</span>';
    } else {
      dom.btnSend.innerHTML = '<svg class="icon"><use href="#i-send"/></svg><span>发送</span>';
    }
    var hint = '回答由 DeepSeek 根据视频转写生成，引用以「MM:SS」标注，点击可跳转播放。';
    if (!job) hint = '';
    else if (job.status === 'error') hint = '分析失败，请先重试作业。';
    else if (isActive(job)) hint = '作业处理中，转写完成后即可提问。';
    else if (!job.has_transcript) hint = '本次作业没有可用的转写内容，暂时无法对话。';
    else if (state.settings && !state.settings.deepseek_api_key_set) {
      hint = '尚未在设置中配置 DeepSeek API Key（若已通过环境变量配置可忽略）。';
    } else if (streaming) hint = '正在生成回答…';
    dom.chatHintText.textContent = hint;
    if (!chat.suggestions || !chat.suggestions.length) renderChatSuggestions();
  }

  function autoGrowInput() {
    var input = dom.chatInput;
    input.style.height = 'auto';
    var height = Math.min(input.scrollHeight, 160);
    input.style.height = Math.max(42, height) + 'px';
  }

  function cancelChatStreaming() {
    if (state.chat && state.chat.controller) {
      try { state.chat.controller.abort(); } catch (e) { /* noop */ }
      state.chat.controller = null;
    }
    if (state.chat) {
      state.chat.streaming = false;
    }
    updateComposer();
  }

  function sendChatMessage(raw) {
    var question = String(raw == null ? '' : raw).trim();
    if (!question) return;
    var job = state.job;
    var chat = state.chat;
    if (!job || !chat) return;
    if (chat.streaming) return;
    if (!canChat(job)) {
      toast('转写完成后才能对话', 'info');
      return;
    }

    chat.streaming = true;
    dom.chatInput.value = '';
    autoGrowInput();
    renderChatSuggestions();
    updateComposer();

    var userMessage = { role: 'user', content: question, citations: [], id: 0 };
    chat.messages.push(userMessage);
    appendMessageNode(userMessage);
    scrollChat(true);

    var assistant = { role: 'assistant', content: '', citations: [], id: 0 };
    chat.messages.push(assistant);
    var node = appendMessageNode({ role: 'assistant', content: '' });
    node.content.innerHTML = '<span class="thinking-dots"><i></i><i></i><i></i></span>';
    scrollChat(true);
    var renderer = createStreamRenderer(node.content, assistant);

    var controller = new AbortController();
    chat.controller = controller;
    var streamError = null;
    var jobId = job.id;

    fetch('/api/jobs/' + encodeURIComponent(jobId) + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify({ question: question, stream: true }),
      signal: controller.signal
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (text) {
          var message = '请求失败（HTTP ' + res.status + '）';
          try {
            var parsed = JSON.parse(text);
            if (parsed && parsed.detail) message = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail);
          } catch (e) { if (text) message = text.slice(0, 300); }
          throw new ApiError(message, res.status);
        });
      }
      if (!res.body) throw new ApiError('浏览器不支持流式响应', 0);
      return consumeSse(res.body, function (event, data) {
        if (event === 'delta') {
          var text = data && (data.text != null ? data.text : data.content);
          if (typeof text === 'string' && text) {
            assistant.content += text;
            renderer.tick();
          }
        } else if (event === 'citations') {
          assistant.citations = (data && Array.isArray(data.items)) ? data.items : [];
          renderCitationChips(node.citations, assistant.citations);
        } else if (event === 'done') {
          assistant.id = (data && data.message_id) || 0;
        } else if (event === 'error') {
          streamError = (data && (data.message || data.detail)) || '生成回答失败';
        }
      });
    }).then(function () {
      if (streamError) throw new ApiError(streamError, 502);
      if (!assistant.content) {
        node.content.innerHTML = '<span class="muted">（没有返回内容）</span>';
      } else {
        renderer.finish();
      }
      if (assistant.citations.length) renderCitationChips(node.citations, assistant.citations);
      node.meta.textContent = 'DeepSeek';
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      renderer.finish();
      node.bubble.classList.add('is-error');
      node.content.innerHTML = '';
      node.content.textContent = '生成回答失败：' + (err && err.message ? err.message : '未知错误');
      node.citations.innerHTML = '';
      if (err && /API Key/i.test(err.message || '')) {
        var goSettings = document.createElement('button');
        goSettings.type = 'button';
        goSettings.className = 'link-btn';
        goSettings.dataset.action = 'open-settings';
        goSettings.textContent = '去设置填写 API Key →';
        node.citations.appendChild(goSettings);
        toast('尚未配置 DeepSeek API Key，请先在设置中填写', 'error', 5200);
      } else {
        toast('回答失败：' + (err.message || '未知错误'), 'error', 4800);
      }
    }).then(function () {
      if (state.chat !== chat || !state.job || state.job.id !== jobId) return;
      chat.streaming = false;
      chat.controller = null;
      updateComposer();
      renderChatSuggestions();
      scrollChat(false);
      if (!dom.chatInput.disabled) dom.chatInput.focus();
    });
  }

  /* =======================================================================
     作业操作
     ======================================================================= */

  function startUpload(file) {
    if (!file) return;
    var validType = (file.type && file.type.indexOf('video/') === 0) ||
      /\.(mp4|mkv|mov|webm|avi|flv|m4v|ts|mpg|mpeg|wmv|3gp)$/i.test(file.name || '');
    if (!validType) {
      toast('请选择视频文件（mp4 / mkv / mov / webm 等）', 'error');
      return;
    }
    if (state.uploadXhr) {
      toast('已有文件正在上传，请稍候', 'info');
      return;
    }
    state.retryFile = file;
    var xhr = new XMLHttpRequest();
    state.uploadXhr = xhr;
    var form = new FormData();
    form.append('file', file, file.name);
    form.append('title', titleFromFilename(file.name));

    dom.uploadProgress.classList.remove('is-hidden');
    dom.dropzone.classList.add('is-busy');
    dom.uploadName.textContent = file.name;
    dom.uploadHint.textContent = '正在上传到本地服务';
    setUploadPct(0);

    xhr.open('POST', '/api/jobs/upload');
    xhr.responseType = 'json';
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) setUploadPct(ev.loaded / ev.total * 100);
    };
    xhr.onload = function () {
      state.uploadXhr = null;
      dom.dropzone.classList.remove('is-busy');
      var payload = xhr.response;
      if (payload == null) {
        try { payload = JSON.parse(xhr.responseText || '{}'); } catch (e) { payload = null; }
      }
      if (xhr.status >= 200 && xhr.status < 300 && payload && payload.job) {
        setUploadPct(100);
        dom.uploadHint.textContent = '上传完成，已开始分析';
        toast('上传完成，已开始分析', 'success');
        setTimeout(hideUploadProgress, 600);
        loadJobs().then(function () { openJob(payload.job.id); });
      } else {
        var message = '上传失败（HTTP ' + xhr.status + '）';
        if (payload && payload.detail) {
          message = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail);
        }
        hideUploadProgress();
        toast(message, 'error', 5200);
      }
    };
    xhr.onerror = function () {
      state.uploadXhr = null;
      dom.dropzone.classList.remove('is-busy');
      hideUploadProgress();
      toast('上传失败：无法连接本地服务，请确认后端已启动', 'error', 5200);
    };
    xhr.onabort = function () {
      state.uploadXhr = null;
      dom.dropzone.classList.remove('is-busy');
      hideUploadProgress();
      toast('已取消上传', 'info', 2400);
    };
    xhr.send(form);
  }

  function setUploadPct(pct) {
    var value = Math.max(0, Math.min(100, Math.round(pct)));
    dom.uploadBar.style.width = value + '%';
    dom.uploadPct.textContent = value + '%';
  }

  function hideUploadProgress() {
    dom.uploadProgress.classList.add('is-hidden');
    dom.dropzone.classList.remove('is-busy');
    setUploadPct(0);
  }

  function cancelUpload() {
    if (state.uploadXhr) {
      try { state.uploadXhr.abort(); } catch (e) { /* noop */ }
    } else {
      hideUploadProgress();
    }
  }

  function analyzeUrl() {
    var url = dom.urlInput.value.trim();
    if (!url) {
      toast('请先粘贴视频页面链接', 'error');
      dom.urlInput.focus();
      return;
    }
    if (!/^https?:\/\//i.test(url)) {
      toast('请输入以 http(s):// 开头的链接', 'error');
      dom.urlInput.focus();
      return;
    }
    dom.btnAnalyzeUrl.disabled = true;
    dom.btnAnalyzeUrl.textContent = '提交中…';
    api('/api/jobs', { method: 'POST', body: { source_type: 'url', url: url } }).then(function (data) {
      if (!data || !data.job) throw new ApiError('后端返回的数据不完整', 0);
      dom.urlInput.value = '';
      toast('已创建作业，开始解析视频', 'success');
      return loadJobs().then(function () { openJob(data.job.id); });
    }).catch(function (err) {
      toast('创建作业失败：' + err.message, 'error', 5200);
    }).then(function () {
      dom.btnAnalyzeUrl.disabled = false;
      dom.btnAnalyzeUrl.textContent = '分析';
    });
  }

  function deleteJob(job) {
    if (!job) return;
    var title = job.title || '未命名视频';
    confirmDialog({
      title: '删除作业',
      message: '确定删除「' + title + '」吗？将同时删除本地生成的转写、总结与画面帧文件（源视频文件不会被删除）。',
      okText: '删除',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      return api('/api/jobs/' + encodeURIComponent(job.id), { method: 'DELETE' }).then(function () {
        toast('已删除作业', 'success');
        if (state.currentJobId === job.id) {
          clearHash();
          closeJob();
        }
        return loadJobs();
      }).catch(function (err) {
        toast('删除失败：' + err.message, 'error', 4600);
      });
    });
  }

  function regenerateSummary() {
    var job = state.job;
    if (!job) return;
    if (isActive(job)) {
      toast('作业正在处理中，请稍候', 'info');
      return;
    }
    dom.btnRegenerate.disabled = true;
    var original = dom.btnRegenerate.querySelector('span');
    if (original) original.textContent = '提交中…';
    api('/api/jobs/' + encodeURIComponent(job.id) + '/regenerate-summary', { method: 'POST', body: {} }).then(function (data) {
      if (data && data.job) {
        state.job = data.job;
        renderJobChrome();
        scheduleJobPoll();
      }
      toast('正在重新生成总结…', 'info');
      if (state.activeTab === 'summary') renderSummaryPanel(true);
    }).catch(function (err) {
      toast('重新生成失败：' + err.message, 'error', 5200);
    }).then(function () {
      dom.btnRegenerate.disabled = false;
      if (original) original.textContent = '重新生成总结';
    });
  }

  function retryJob() {
    var job = state.job;
    if (!job) return;
    var sourceUrl = job.source_url || '';
    if (job.source_type === 'file') {
      if (state.retryFile) {
        startUpload(state.retryFile);
      } else {
        toast('本地文件作业无法直接重试，请重新把视频文件拖入左栏分析', 'error', 5600);
      }
      return;
    }
    if (!/^https?:\/\//i.test(sourceUrl)) {
      toast('缺少原始视频链接，无法重试，请重新提交链接', 'error', 5200);
      return;
    }
    dom.btnRetryJob.disabled = true;
    api('/api/jobs', { method: 'POST', body: { source_type: 'url', url: sourceUrl } }).then(function (data) {
      if (!data || !data.job) throw new ApiError('后端返回的数据不完整', 0);
      toast('已重新创建作业', 'success');
      return loadJobs().then(function () { openJob(data.job.id); });
    }).catch(function (err) {
      toast('重试失败：' + err.message, 'error', 5200);
    }).then(function () {
      dom.btnRetryJob.disabled = false;
    });
  }

  /* =======================================================================
     事件绑定
     ======================================================================= */

  function handleAction(action, element) {
    switch (action) {
      case 'open-settings':
        openSettings();
        break;
      case 'retry-job':
        retryJob();
        break;
      case 'regenerate':
        regenerateSummary();
        break;
      case 'retry-list':
        loadJobs().then(function (ok) {
          if (ok) toast('作业列表已刷新', 'success', 2200);
        });
        break;
      case 'retry-health':
        checkHealthManual();
        break;
      case 'delete-job':
        if (state.job) deleteJob(state.job);
        break;
      default:
        break;
    }
  }

  function handleGlobalClick(ev) {
    var target = ev.target;
    if (!target || !target.closest) return;

    var actionEl = target.closest('[data-action]');
    if (actionEl) {
      ev.preventDefault();
      handleAction(actionEl.dataset.action, actionEl);
      return;
    }

    var keywordEl = target.closest('[data-keyword]');
    if (keywordEl) {
      ev.preventDefault();
      filterTranscriptByKeyword(keywordEl.dataset.keyword || '');
      return;
    }

    var row = target.closest('.tx-row');
    if (row) {
      ev.preventDefault();
      jumpToRow(row);
      return;
    }

    var seekEl = target.closest('[data-seek]');
    if (seekEl) {
      ev.preventDefault();
      seekTo(Number(seekEl.dataset.seek));
      if (seekEl.dataset.segmentId) highlightTranscriptSegment(Number(seekEl.dataset.segmentId));
      if (seekEl.id === 'lightboxTime') closeModal(dom.lightbox);
      return;
    }

    var closeEl = target.closest('[data-close]');
    if (closeEl) {
      ev.preventDefault();
      closeByAction(closeEl.dataset.close);
      return;
    }

    if (!target.closest('.dropdown')) {
      var menu = $('#exportMenu');
      if (menu) menu.classList.add('is-hidden');
      state.exportOpen = false;
    }
  }

  function filterTranscriptByKeyword(keyword) {
    state.transcriptFilter = String(keyword || '');
    setActiveTab('transcript');
    ensureActiveTab(false).then(function () {
      var search = $('#transcriptSearch');
      if (search) {
        search.value = state.transcriptFilter;
        search.focus();
        search.select();
      }
    });
  }

  function bindEvents() {
    document.addEventListener('click', handleGlobalClick);

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') {
        if (state.openModals.length) {
          ev.preventDefault();
          closeTopModal();
        }
      }
    });

    dom.statusPill.addEventListener('click', checkHealthManual);
    dom.btnSettings.addEventListener('click', openSettings);
    dom.btnEmptySettings.addEventListener('click', openSettings);
    dom.btnEmptyUpload.addEventListener('click', function () { dom.fileInput.click(); });

    dom.fileInput.addEventListener('change', function () {
      var file = dom.fileInput.files && dom.fileInput.files[0];
      if (file) startUpload(file);
      dom.fileInput.value = '';
    });

    dom.dropzone.addEventListener('click', function () { dom.fileInput.click(); });
    dom.dropzone.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        dom.fileInput.click();
      }
    });
    ['dragenter', 'dragover'].forEach(function (name) {
      dom.dropzone.addEventListener(name, function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        dom.dropzone.classList.add('is-dragover');
      });
    });
    ['dragleave', 'dragend'].forEach(function (name) {
      dom.dropzone.addEventListener(name, function (ev) {
        ev.preventDefault();
        dom.dropzone.classList.remove('is-dragover');
      });
    });
    dom.dropzone.addEventListener('drop', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      dom.dropzone.classList.remove('is-dragover');
      var file = ev.dataTransfer && ev.dataTransfer.files ? ev.dataTransfer.files[0] : null;
      if (file) startUpload(file);
    });
    document.addEventListener('dragover', function (ev) { ev.preventDefault(); });
    document.addEventListener('drop', function (ev) { ev.preventDefault(); });

    dom.btnCancelUpload.addEventListener('click', cancelUpload);
    dom.btnAnalyzeUrl.addEventListener('click', analyzeUrl);
    dom.urlInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        analyzeUrl();
      }
    });

    dom.btnRefreshJobs.addEventListener('click', function () {
      loadJobs().then(function (ok) {
        if (ok) toast('作业列表已刷新', 'success', 2200);
      });
    });

    dom.btnDeleteJob.addEventListener('click', function () { if (state.job) deleteJob(state.job); });
    dom.btnRegenerate.addEventListener('click', regenerateSummary);
    dom.btnRetryJob.addEventListener('click', retryJob);

    $$('.tab').forEach(function (btn) {
      btn.addEventListener('click', function () { setActiveTab(btn.dataset.tab); });
    });

    // 播放器
    dom.video.addEventListener('loadedmetadata', function () {
      onVideoReady();
      if (state.pendingSeek != null) {
        try { dom.video.currentTime = Math.min(state.pendingSeek, dom.video.duration || state.pendingSeek); } catch (e) { /* noop */ }
        state.pendingSeek = null;
      }
    });
    dom.video.addEventListener('canplay', onVideoReady);
    dom.video.addEventListener('error', onVideoError);
    dom.video.addEventListener('timeupdate', onTimeUpdate);

    // 设置弹窗
    dom.btnToggleKey.addEventListener('click', function () {
      var isPassword = dom.setApiKey.type === 'password';
      dom.setApiKey.type = isPassword ? 'text' : 'password';
      dom.keyEyeIcon.querySelector('use').setAttribute('href', isPassword ? '#i-eye-off' : '#i-eye');
    });
    dom.setModel.addEventListener('change', function () {
      var custom = dom.setModel.value === '__custom__';
      dom.setModelCustom.classList.toggle('is-hidden', !custom);
      if (custom) dom.setModelCustom.focus();
    });
    dom.btnTestKey.addEventListener('click', testConnection);
    dom.btnSaveSettings.addEventListener('click', function () { saveSettings(false); });
    dom.setVisionEnabled.addEventListener('change', function () {
      if (dom.setVisionEnabled.checked) {
        var section = $('#visionSection');
        if (section) section.open = true;
      }
    });
    $$('#settingsModal [data-close]').forEach(function (el) {
      el.addEventListener('click', function () { closeModal(dom.settingsModal); });
    });

    // 确认弹窗
    dom.btnConfirmOk.addEventListener('click', function () {
      if (state.confirmResolver) {
        var resolve = state.confirmResolver;
        state.confirmResolver = null;
        closeModal(dom.confirmModal);
        resolve(true);
      }
    });
    dom.btnConfirmCancel.addEventListener('click', function () { closeModal(dom.confirmModal); });
    $$('#confirmModal [data-close]').forEach(function (el) {
      el.addEventListener('click', function () { closeModal(dom.confirmModal); });
    });

    // 灯箱
    $$('#lightbox [data-close]').forEach(function (el) {
      el.addEventListener('click', function () { closeModal(dom.lightbox); });
    });

    // 对话输入
    dom.chatInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
        ev.preventDefault();
        sendChatMessage(dom.chatInput.value);
      }
    });
    dom.chatInput.addEventListener('input', function () {
      autoGrowInput();
      updateComposer();
    });
    dom.btnSend.addEventListener('click', function () { sendChatMessage(dom.chatInput.value); });
    dom.chatLog.addEventListener('scroll', function () { /* 保留滚动位置，不自动吸底 */ });

    // 路由
    window.addEventListener('hashchange', function () {
      var id = parseHashJobId();
      if (!id) {
        if (state.currentJobId) closeJob();
        return;
      }
      if (id === state.currentJobId && state.job) return;
      openJob(id, { fromHash: true });
    });

    // 页面重新可见时刷新
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) return;
      refreshHealth({ autoLoad: true }).then(function () {
        if (state.wasOnline && !state.job) loadJobs();
      });
    });
    window.addEventListener('focus', function () {
      clearTimeout(state.focusTimer);
      state.focusTimer = setTimeout(function () {
        if (state.wasOnline && !state.job) loadJobs();
      }, 800);
    });

    // 兜底错误日志（不吞错，只记录，便于排查）
    window.addEventListener('error', function (ev) {
      console.error('[视频分析] 未捕获错误：', ev.error || ev.message);
    });
    window.addEventListener('unhandledrejection', function (ev) {
      console.error('[视频分析] 未处理的 Promise 拒绝：', ev.reason);
    });
  }

  /* =======================================================================
     启动
     ======================================================================= */

  function boot() {
    bindEvents();
    resetPlayer();
    setView('empty');
    refreshHealth().then(function () {
      if (!state.wasOnline) {
        renderJobListError(new ApiError('无法连接本地服务', 0));
      }
      openJobFromHashIfNeeded();
    });
    state.healthTimer = setInterval(function () {
      if (document.hidden) return;
      refreshHealth({ autoLoad: false });
    }, 12000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
