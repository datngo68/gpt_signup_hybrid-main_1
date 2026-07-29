/* gpt_signup_hybrid — frontend logic */
(() => {
  'use strict';

  // ── Auth ────────────────────────────────────────────────────────────
  const _LS_TOKEN = 'gpt_reg.auth_token';

  function getAuthToken() {
    // 1. Meta tag (injected server-side khi loopback bind)
    const meta = document.querySelector('meta[name="auth-token"]');
    const metaVal = (meta && meta.content) || '';
    if (metaVal) return metaVal;
    // 2. URL query param ?token=... (cho non-loopback access)
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get('token') || '';
    if (urlToken) {
      localStorage.setItem(_LS_TOKEN, urlToken);
      return urlToken;
    }
    // 3. localStorage (previously entered)
    return localStorage.getItem(_LS_TOKEN) || '';
  }

  // ── SseBus — unified SSE multiplexer (frontend) ─────────────────
  // Dùng fetch(POST)+ReadableStream thay EventSource: Cloudflare Quick Tunnel
  // buffer SSE qua GET và chỉ flush khi đóng connection
  // (cloudflare/cloudflared#1449) → POST stream real-time. Token đi qua header
  // X-API-Token (không lộ ở query log như EventSource ?token=).
  const SseBus = (() => {
    let _active = false;
    let _abort = null;
    let _reconnectTimer = null;
    const _handlers = new Map(); // channel -> [callback, ...]

    function _dispatchEvent(raw) {
      // 1 SSE event (đã tách bằng "\n\n"); gộp các dòng "data:" theo spec.
      const dataLines = [];
      for (const line of raw.split('\n')) {
        if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
        // dòng bắt đầu ':' là comment/heartbeat (": ping") → bỏ qua
      }
      if (dataLines.length === 0) return;
      let data;
      try { data = JSON.parse(dataLines.join('\n')); } catch (_) { return; }
      const channel = data.channel;
      if (!channel) return;
      const cbs = _handlers.get(channel);
      if (cbs) cbs.forEach(cb => cb(data));
    }

    async function _stream() {
      const token = getAuthToken();
      const headers = { 'Accept': 'text/event-stream' };
      if (token) headers['X-API-Token'] = token;
      _abort = new AbortController();
      const resp = await fetch('/api/sse', {
        method: 'POST',
        headers,
        cache: 'no-store',
        signal: _abort.signal,
      });
      if (!resp.ok || !resp.body) throw new Error('SSE HTTP ' + resp.status);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          _dispatchEvent(buf.slice(0, idx));
          buf = buf.slice(idx + 2);
        }
      }
    }

    function _scheduleReconnect() {
      if (!_active || _reconnectTimer) return;
      _reconnectTimer = setTimeout(() => {
        _reconnectTimer = null;
        _run();
      }, 3000);
    }

    function _run() {
      if (!_active) return;
      // Stream vô hạn: resolve (done) nghĩa là connection rớt → reconnect.
      _stream().catch(() => {}).finally(() => {
        _abort = null;
        _scheduleReconnect();
      });
    }

    function connect() {
      if (_active) return;
      _active = true;
      _run();
    }

    function on(channel, callback) {
      if (!_handlers.has(channel)) _handlers.set(channel, []);
      _handlers.get(channel).push(callback);
    }

    return { connect, on };
  })();
  window.SseBus = SseBus;

  // ── LocalStorage keys ─────────────────────────────────────────────
  // NOTE: Chỉ textarea drafts giữ ở localStorage (ngoài scope unified-settings-store).
  // Tất cả runtime config đã migrate sang Settings store (DB-backed).
  const LS_INPUT_REG = 'gpt_reg.input.reg';

  // Helper: persist textarea content vào localStorage. Lưu cả khi rỗng để
  // phân biệt "user đã xoá tay" vs "chưa từng nhập" — chỉ xoá key khi
  // user bấm Clear Input.
  function persistTextarea(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* quota — bỏ qua */ }
  }
  function clearPersistedTextarea(key) {
    try { localStorage.removeItem(key); } catch (e) { /* ignore */ }
  }
  // Expose để các tab khác (session.js, link.js) dùng chung pattern
  window.GptUi = Object.assign(window.GptUi || {}, {
    persistTextarea,
    clearPersistedTextarea,
  });

  // ── Error alert sound (Web Audio API — works in background tabs) ──
  let _audioCtx = null;
  function _getAudioCtx() {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (_audioCtx.state === 'suspended') _audioCtx.resume();
    return _audioCtx;
  }
  function playErrorAlert() {
    try {
      const ctx = _getAudioCtx();
      const now = ctx.currentTime;
      // 3 beeps: 880Hz, loud, short — unmissable
      for (let i = 0; i < 3; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'square';
        osc.frequency.value = 880;
        gain.gain.value = 0.5;
        const t = now + i * 0.25;
        osc.start(t);
        osc.stop(t + 0.15);
      }
    } catch (e) { /* AudioContext not available */ }
  }
  // Unlock AudioContext on first user interaction (required by browsers)
  function _unlockAudio() {
    try { _getAudioCtx(); } catch (e) { }
    document.removeEventListener('click', _unlockAudio);
    document.removeEventListener('keydown', _unlockAudio);
  }
  document.addEventListener('click', _unlockAudio);
  document.addEventListener('keydown', _unlockAudio);

  // Success alert sound — ascending fanfare, loud & celebratory
  function playSuccessAlert() {
    try {
      const ctx = _getAudioCtx();
      const now = ctx.currentTime;
      // 5-note ascending fanfare: C5 → E5 → G5 → C6 → E6, loud
      const notes = [523, 659, 784, 1047, 1319];
      for (let i = 0; i < notes.length; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'square';
        osc.frequency.value = notes[i];
        gain.gain.value = 0.6;
        const t = now + i * 0.15;
        osc.start(t);
        gain.gain.setValueAtTime(0.6, t);
        gain.gain.linearRampToValueAtTime(0, t + 0.2);
        osc.stop(t + 0.2);
      }
      // Final sustained chord (C5+E5+G5) — big finish
      const chord = [523, 659, 784];
      for (const freq of chord) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.value = 0.4;
        const t = now + notes.length * 0.15;
        osc.start(t);
        gain.gain.setValueAtTime(0.4, t);
        gain.gain.linearRampToValueAtTime(0, t + 0.6);
        osc.stop(t + 0.6);
      }
    } catch (e) { /* AudioContext not available */ }
  }

  // Expose for session.js, link.js, upi.js
  window.GptUi = Object.assign(window.GptUi || {}, { playErrorAlert, playSuccessAlert });

  // ── State ─────────────────────────────────────────────────────────
  const state = {
    jobs: new Map(),          // id → job dict
    order: [],                // job id order
    activeJobId: null,        // job đang xem log
    maxConcurrent: 3,
    mode: 'multi',
    headless: true,
    debug: false,
    useProxy: true,
    mailModes: [],            // [{id, label, input_placeholder, input_help, config_schema}]
    currentMailMode: 'icloud_v3',
  };

  // ── DOM refs ──────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput: $('combo-input'),
    btnRun: $('btn-run'),
    btnStopAll: $('btn-stop-all'),
    btnClearInput: $('btn-clear-input'),
    comboCount: $('combo-count'),
    defaultPassword: $('default-password'),
    jobTimeout: $('job-timeout'),
    autoRetryMax: $('auto-retry-max'),
    jobList: $('job-list'),
    jobSummary: $('job-summary'),
    logPane: $('log-pane'),
    logTarget: $('log-target'),
    successPane: $('success-pane'),
    errorPane: $('error-pane'),
    btnCopySuccess: $('btn-copy-success'),
    btnCopyError: $('btn-copy-error'),
    statusPill: $('status-pill'),
    modeSelect: $('mode'),
    headlessToggle: $('headless-toggle'),
    debugToggle: $('debug-toggle'),
    proxyToggle: $('proxy-toggle'),
    inputHint: $('input-hint'),
    mailModeSelect: $('mail-mode-select'),
    regModeSelect: $('reg-mode-select'),
    mailModeConfigHost: $('mail-mode-config-host'),
  };

  // ── Helpers ───────────────────────────────────────────────────────
  const icons = Object.freeze({
    stop: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
    retry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
    remove: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11 4"/><path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 1 0 7.07 7.07L13 19"/></svg>',
    token: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 2l-2 2"/><path d="M7.61 13.39a5.5 5.5 0 1 0 7.78 7.78L21 15.5l-7.5-7.5-5.89 5.39Z"/><path d="m14.5 6.5 3 3"/></svg>',
    eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
    eyeOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 3 18 18"/><path d="M10.6 10.6A2 2 0 0 0 13.4 13.4"/><path d="M9.9 4.2A10.6 10.6 0 0 1 12 4c6.5 0 10 8 10 8a18.7 18.7 0 0 1-3.1 4.3"/><path d="M6.1 6.1C3.4 8 2 12 2 12s3.5 8 10 8a10.7 10.7 0 0 0 5.9-1.8"/></svg>',
    qr: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><line x1="14" y1="14" x2="14" y2="17"/><line x1="14" y1="20" x2="14" y2="21"/><line x1="17" y1="14" x2="21" y2="14"/><line x1="17" y1="17" x2="17" y2="21"/><line x1="20" y1="17" x2="21" y2="17"/><line x1="20" y1="20" x2="21" y2="20"/></svg>',
    verify: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 5 3.4 7.7 8 9 4.6-1.3 8-4 8-9V5l-8-3Z"/><polyline points="9 12 11 14 15 10"/></svg>',
  });
  const mailModeUiCopy = Object.freeze({
    icloud_v3: {
      input_help: 'One iCloud v3 entry per line: email|api_url (Worker v2 readmail URL). Need iCloud mail? Contact @prr9293 on Telegram (https://t.me/prr9293) to buy.',
      input_placeholder: '⚡ Contact @prr9293 on Telegram (https://t.me/prr9293) to buy iCloud mail.\n\nFormat — one entry per line: email|api_url\nExample:\npetunia-boar-3d+hblx3n@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/<token>/data',
    },
    outlook: {
      input_help: 'One Outlook combo per line.',
      input_placeholder: 'email|password|refresh_token|client_id',
    },
    worker: {
      input_help: 'One iCloud email per line via Worker OTP. Need iCloud mail? Contact @prr9293 on Telegram (https://t.me/prr9293) to buy.',
      input_placeholder: '⚡ Contact @prr9293 on Telegram (https://t.me/prr9293) to buy iCloud mail.\n\nFormat — one iCloud email per line:\nuser@icloud.com',
    },
    gmail_advanced: {
      input_help: 'Mỗi dòng: api_url hoặc email|api_url. Pre-check mail_status=live.',
      input_placeholder: 'https://checkgmail.live/otp/...\nbrandonspencer7424@gmail.com|https://checkgmail.live/otp/...',
    },
  });

  function fmtDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return secs.toFixed(1) + 's';
    return Math.floor(secs / 60) + 'm' + Math.floor(secs % 60) + 's';
  }

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function api(path, opts = {}) {
    const token = getAuthToken();
    const headers = {
      'Content-Type': 'application/json',
      ...(token ? { 'X-API-Token': token } : {}),
      ...(opts.headers || {}),
    };
    return fetch(path, {
      ...opts,
      headers,
    }).then((r) => {
      if (!r.ok) return r.text().then((t) => { throw new Error(`HTTP ${r.status}: ${t}`); });
      return r.json();
    });
  }

  function icon(name) {
    return icons[name] || '';
  }

  // ── Toast (reusable, top-right) ───────────────────────────────────
  // GptUi.toast(message, { type, duration }) — type: success|error|info|warn.
  // Container tạo lazy 1 lần, stack dọc, auto-dismiss + click để đóng sớm.
  // Dùng chung mọi tab: window.GptUi.toast('Đã copy', { type: 'success' }).
  const _TOAST_ICONS = Object.freeze({
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  });

  let _toastContainer = null;
  function _ensureToastContainer() {
    if (_toastContainer && document.body.contains(_toastContainer)) return _toastContainer;
    let el = document.getElementById('gpt-toast-container');
    if (!el) {
      el = document.createElement('div');
      el.id = 'gpt-toast-container';
      el.className = 'gpt-toast-container';
      el.setAttribute('aria-live', 'polite');
      el.setAttribute('aria-atomic', 'false');
      document.body.appendChild(el);
    }
    _toastContainer = el;
    return el;
  }

  function toast(message, opts) {
    opts = opts || {};
    if (!message) return null;
    const type = _TOAST_ICONS[opts.type] ? opts.type : 'success';
    const duration = typeof opts.duration === 'number' ? opts.duration : 2600;
    const container = _ensureToastContainer();

    const el = document.createElement('div');
    el.className = 'gpt-toast gpt-toast-' + type;
    el.setAttribute('role', 'status');

    const ic = document.createElement('span');
    ic.className = 'gpt-toast-icon';
    ic.innerHTML = _TOAST_ICONS[type];

    const msg = document.createElement('span');
    msg.className = 'gpt-toast-msg';
    msg.textContent = message;

    el.appendChild(ic);
    el.appendChild(msg);
    container.appendChild(el);

    requestAnimationFrame(() => el.classList.add('gpt-toast-show'));

    let timer = null;
    const remove = () => {
      if (timer) { clearTimeout(timer); timer = null; }
      el.classList.remove('gpt-toast-show');
      el.classList.add('gpt-toast-hide');
      setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 240);
    };
    timer = setTimeout(remove, duration);
    el.addEventListener('click', remove);
    return el;
  }

  // Copy text + toast 1 phát (DRY cho mọi nút copy). Trả Promise.
  function copyWithToast(text, message, opts) {
    return copyText(text).then(() => {
      toast(message || 'Đã copy', Object.assign({ type: 'success' }, opts || {}));
    }).catch((err) => {
      toast('Copy thất bại', { type: 'error' });
      throw err;
    });
  }

  function copyText(text) {
    // Fallback cho non-HTTPS / mobile browsers
    function fallbackCopy(str) {
      const ta = document.createElement('textarea');
      ta.value = str;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (_) { /* ignore */ }
      document.body.removeChild(ta);
      return ok;
    }

    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(() => {
        if (fallbackCopy(text)) return;
        return Dialog.alert({ message: 'Copy failed.' }).then(() => { throw new Error('copy failed'); });
      });
    }
    // Non-secure context: dùng fallback trực tiếp
    if (fallbackCopy(text)) return Promise.resolve();
    return Dialog.alert({ message: 'Copy failed.' }).then(() => { throw new Error('copy failed'); });
  }

  let _activeTabId = null;
  // Hide-Reg launch mode: server inject body[data-hide-reg="1"] khi chạy
  // `web --hide-reg`. Frontend ẩn tab Reg (CSS), giữ các tab khác.
  function _isHideRegMode() {
    return document.body.dataset.hideReg === '1';
  }
  function activateTab(tabId) {
    const prevTab = _activeTabId;
    _activeTabId = tabId;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    document.querySelectorAll('.tab-content').forEach((tab) => {
      tab.classList.toggle('active', tab.id === `tab-${tabId}`);
    });
    Settings.save('ui.active_tab', tabId, getAuthToken());
    document.dispatchEvent(new CustomEvent('gpt:tab', { detail: { tab: tabId, prev: prevTab } }));
  }

  function initTabs() {
    if (document.body.dataset.tabsBound === 'true') return;
    document.body.dataset.tabsBound = 'true';
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => activateTab(btn.dataset.tab));
    });
    // Tab tạm ẩn (chưa dùng được). Mở lại: bỏ khỏi danh sách + bỏ comment nút nav trong index.html.
    const hiddenTabs = ['link', 'hme'];
    // Hide-Reg mode: thêm 'reg' vào danh sách ẩn để không khởi tạo vào tab Reg.
    if (_isHideRegMode()) hiddenTabs.push('reg');
    let initialTab = Settings.get('ui.active_tab') || document.querySelector('.tab-btn.active')?.dataset.tab || 'reg';
    if (hiddenTabs.includes(initialTab)) initialTab = _isHideRegMode() ? 'session' : 'reg';
    activateTab(initialTab);
  }

  window.GptUi = Object.assign(window.GptUi || {}, {
    icon,
    copyText,
    toast,
    copyWithToast,
    activateTab,
    initTabs,
    getAuthToken,
  });

  // ── Combo counter ─────────────────────────────────────────────────
  function updateComboCount() {
    const lines = dom.comboInput.value.split('\n').filter((l) => {
      const s = l.trim();
      return s && !s.startsWith('#');
    });
    dom.comboCount.textContent = `${lines.length} combo${lines.length === 1 ? '' : 's'}`;
  }

  dom.comboInput.addEventListener('input', () => {
    updateComboCount();
    persistTextarea(LS_INPUT_REG, dom.comboInput.value);
  });

  // ── Render job list ──────────────────────────────────────────────
  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">No jobs yet. Paste combos and click Run.</div>';
      dom.jobSummary.textContent = '0 total';
      return;
    }

    const stats = { queued: 0, running: 0, success: 0, error: 0, cancelled: 0 };
    const html = state.order.map((id, idx) => {
      const j = state.jobs.get(id);
      if (!j) return '';
      stats[j.status] = (stats[j.status] || 0) + 1;
      const cls = state.activeJobId === id ? 'job is-active' : 'job';
      const actionBtn = j.status === 'running'
        ? `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${icon('stop')}</button>`
        : `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${icon('retry')}</button>`;
      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(j.status)}">${escHtml(j.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(j.email)}">${escHtml(j.email)}<span class="badge-mode badge-mode-${escHtml(j.mail_mode || 'outlook')}">${escHtml(j.mail_mode || 'outlook')}</span></div>
          </div>
          <div class="job-duration">${escHtml(fmtDuration(j.duration))}</div>
          <div class="job-actions">
            ${actionBtn}
            <button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${icon('remove')}</button>
          </div>
        </div>
      `;
    }).join('');

    dom.jobList.innerHTML = html;
    dom.jobSummary.textContent = [
      `${state.order.length} total`,
      stats.running ? `${stats.running} running` : '',
      stats.queued ? `${stats.queued} queued` : '',
      stats.success ? `${stats.success} done` : '',
      stats.error ? `${stats.error} failed` : '',
    ].filter(Boolean).join(' · ');

    updateStatusPill(stats);
  }

  function updateStatusPill(stats) {
    if (stats.running > 0) {
      dom.statusPill.className = 'pill pill-running';
      dom.statusPill.textContent = `running ${stats.running}/${state.maxConcurrent}`;
    } else if (stats.queued > 0) {
      dom.statusPill.className = 'pill pill-running';
      dom.statusPill.textContent = `queued ${stats.queued}`;
    } else if (stats.error > 0 && stats.success === 0) {
      dom.statusPill.className = 'pill pill-error';
      dom.statusPill.textContent = 'error';
    } else if (stats.success > 0) {
      dom.statusPill.className = 'pill pill-success';
      dom.statusPill.textContent = `done ${stats.success}`;
    } else {
      dom.statusPill.className = 'pill pill-idle';
      dom.statusPill.textContent = 'idle';
    }
  }

  // ── Render success/error output ──────────────────────────────────
  // Secrets không còn nằm trong job snapshot — fetch riêng qua /api/jobs/secrets.
  // Cache local để tránh round-trip mỗi render; refresh khi snapshot/SSE-job update.
  const secretsCache = new Map(); // job_id → {password, secret, first_code, session_path}
  let _secretsRefreshScheduled = false;

  async function refreshSecrets() {
    try {
      const data = await api('/api/jobs/secrets');
      secretsCache.clear();
      const map = data.secrets || {};
      for (const id of Object.keys(map)) {
        secretsCache.set(id, map[id] || {});
      }
      renderOutputs();
    } catch (err) {
      console.warn('refreshSecrets failed', err.message);
    }
  }

  function scheduleSecretsRefresh() {
    if (_secretsRefreshScheduled) return;
    _secretsRefreshScheduled = true;
    // Coalesce nhiều SSE update gần nhau — fetch 1 lần sau 250ms
    setTimeout(() => {
      _secretsRefreshScheduled = false;
      refreshSecrets();
    }, 250);
  }

  function renderOutputs() {
    const successLines = [];
    const errorLines = [];
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j) continue;
      const sec = secretsCache.get(id) || {};
      const password = sec.password || '';
      const secret = sec.secret || '';
      if (j.status === 'success' && secret) {
        successLines.push(`${j.email}|${password}|${secret}`);
      } else if (j.status === 'error') {
        // Signup OK nhưng 2FA fail (job.has_password=true, has_secret=false) → vẫn xuất
        if (password) {
          successLines.push(`${j.email}|${password}|no_2fa`);
        }
        errorLines.push(`${j.email}  →  ${j.error || 'unknown'}`);
      }
    }
    dom.successPane.textContent = successLines.length
      ? successLines.join('\n')
      : 'Format: email|password|secret_2fa';
    dom.errorPane.textContent = errorLines.length
      ? errorLines.join('\n')
      : 'No errors yet.';
  }

  // ── Render log của 1 job ─────────────────────────────────────────
  function renderLog(jobId) {
    if (!jobId) {
      dom.logPane.textContent = '';
      dom.logTarget.textContent = '-';
      return;
    }
    const j = state.jobs.get(jobId);
    if (!j) return;
    dom.logTarget.textContent = j.email;
    api(`/api/jobs/${jobId}/log`).then((data) => {
      const lines = data.log || [];
      // Mỗi span tự kết thúc bằng '\n' (giống applyLog) để SSE append sau
      // không bị dính vào span cuối.
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal)/i.test(l)
          ? 'log-line-error'
          : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}\n</span>`;
      }).join('');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── Job actions ──────────────────────────────────────────────────
  dom.jobList.addEventListener('click', async (e) => {
    const target = e.target;
    const actionBtn = target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      e.stopPropagation();

      if (action === 'retry') {
        if (!(await Dialog.confirm({ message: 'Retry this job?' }))) return;
        api(`/api/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'stop') {
        if (!(await Dialog.confirm({ message: 'Stop this running job?' }))) return;
        api(`/api/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'remove') {
        if (!(await Dialog.confirm({ message: 'Remove this job from the list and textarea?' }))) return;
        const j = state.jobs.get(id);
        if (j) removeFromTextarea(j.email);
        api(`/api/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      }
      return;
    }
    const row = target.closest('.job');
    if (row) {
      state.activeJobId = row.dataset.id;
      renderJobs();
      renderLog(state.activeJobId);
    }
  });

  function removeFromTextarea(email) {
    const lines = dom.comboInput.value.split('\n');
    const filtered = lines.filter((l) => {
      const m = l.trim().split('|')[0];
      return m.toLowerCase() !== email.toLowerCase();
    });
    dom.comboInput.value = filtered.join('\n');
    updateComboCount();
    persistTextarea(LS_INPUT_REG, dom.comboInput.value);
  }

  // ── Mode → concurrency mapping ────────────────────────────────────
  // Mỗi tab có default + cap (max) riêng — UI render options theo cap, lưu
  // key Settings store theo tab. Backend chỉ validate enum, cap kiểm soát FE.
  const _ALL_MODE_OPTIONS = Object.freeze([
    { value: 'single',   label: 'Single (1)',  n: 1 },
    { value: 'multi',    label: 'Multi (2)',   n: 2 },
    { value: 'multi3',   label: 'Multi (3)',   n: 3 },
    { value: 'multi5',   label: 'Multi (5)',   n: 5 },
    { value: 'multi10',  label: 'Multi (10)',  n: 10 },
    { value: 'multi20',  label: 'Multi (20)',  n: 20 },
    { value: 'multi30',  label: 'Multi (30)',  n: 30 },
    { value: 'multi50',  label: 'Multi (50)',  n: 50 },
    { value: 'multi100', label: 'Multi (100)', n: 100 },
    { value: 'multi200', label: 'Multi (200)', n: 200 },
  ]);
  const MODE_TAB_CONFIG = Object.freeze({
    reg:     { defaultMode: 'multi10', cap: 30 },
    session: { defaultMode: 'multi10', cap: 30 },
    upi:     { defaultMode: 'multi30', cap: 200 },
  });

  function _modeToConcurrency(mode) {
    const opt = _ALL_MODE_OPTIONS.find(o => o.value === mode);
    return opt ? opt.n : 1;
  }

  function _renderModeOptionsForTab(tabId) {
    const cfg = MODE_TAB_CONFIG[tabId];
    const wrap = dom.modeSelect.closest('.select-wrap') || dom.modeSelect.parentElement;
    if (!cfg) {
      // Tab không có mode (settings, link, hme...) → ẩn select hoàn toàn
      if (wrap) wrap.style.display = 'none';
      return;
    }
    if (wrap) wrap.style.display = '';
    const html = _ALL_MODE_OPTIONS
      .filter(o => o.n <= cfg.cap)
      .map(o => `<option value="${o.value}">${o.label}</option>`)
      .join('');
    dom.modeSelect.innerHTML = html;
  }

  function _loadModeForTab(tabId) {
    const cfg = MODE_TAB_CONFIG[tabId];
    if (!cfg) return null;
    const validValues = _ALL_MODE_OPTIONS.filter(o => o.n <= cfg.cap).map(o => o.value);
    const saved = Settings.get(`${tabId}.mode`);
    if (saved && validValues.includes(saved)) return saved;
    return cfg.defaultMode;
  }

  let _regModeSyncedOnLoad = false;

  async function _syncRegConcurrencyToServer(mode) {
    const target = _modeToConcurrency(mode);
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ max_concurrent: target }),
      });
      state.maxConcurrent = target;
    } catch (err) {
      console.error('[reg.mode sync]', err);
    }
  }

  function _applyTabMode(tabId) {
    _renderModeOptionsForTab(tabId);
    const cfg = MODE_TAB_CONFIG[tabId];
    if (!cfg) return;
    const mode = _loadModeForTab(tabId);
    dom.modeSelect.value = mode;
    if (tabId === 'reg') {
      state.mode = mode;
      // Force sync server-side config 1 lần khi load tab Reg đầu tiên — tránh
      // case DB key `reg.max_concurrent` còn giá trị stale từ build cũ
      // (clamp 5) dù `reg.mode` đã là multi10/multi20/multi30.
      if (!_regModeSyncedOnLoad) {
        _regModeSyncedOnLoad = true;
        _syncRegConcurrencyToServer(mode);
      }
    }
  }

  // Switch tab → re-render Mode dropdown theo cap + load value đã save cho tab.
  // Listener bind sớm để bắt event 'gpt:tab' đầu tiên dispatch từ initTabs().
  document.addEventListener('gpt:tab', (e) => {
    _applyTabMode(e.detail && e.detail.tab);
  });

  // ── Run button ───────────────────────────────────────────────────
  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) {
      await Dialog.alert({ message: 'Paste combos first.' });
      return;
    }
    dom.btnRun.disabled = true;
    try {
      // Luôn sync config server trước khi chạy
      const target = _modeToConcurrency(state.mode);
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ max_concurrent: target }),
      });
      state.maxConcurrent = target;

      // Build payload theo mail mode
      const payload = {
        combos,
        default_password: dom.defaultPassword.value.trim() || null,
        mail_mode: state.currentMailMode,
        reg_mode: dom.regModeSelect.value || 'browser',
      };
      if (state.currentMailMode === 'worker') {
        // Đọc trực tiếp từ DOM input (không chỉ localStorage — user có thể chưa trigger persist)
        const urlInp = dom.mailModeConfigHost.querySelector('input[data-config-key="logs_url"]');
        const keyInp = dom.mailModeConfigHost.querySelector('input[data-config-key="api_key"]');
        payload.email_logs_url = (urlInp && urlInp.value.trim()) || '';
        payload.email_api_key = (keyInp && keyInp.value.trim()) || '';
      }

      await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
      validateWorkerConfig();
    }
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    clearPersistedTextarea(LS_INPUT_REG);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Stop all running or queued jobs?' }))) return;
    try {
      const res = await api('/api/jobs/stop-all', { method: 'POST' });
      console.log('stopped:', res.stopped);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  document.getElementById('btn-clear-done').addEventListener('click', async () => {
    try {
      const res = await api('/api/jobs/clear-finished', { method: 'POST' });
      // Refresh list (SSE sẽ broadcast clear_finished event)
      console.log('cleared:', res.removed);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  document.getElementById('btn-clear-all').addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.', danger: true, confirmLabel: 'Xoá' }))) return;
    try {
      const res = await api('/api/jobs/clear-all', { method: 'POST' });
      console.log('clear-all:', res.removed);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  document.getElementById('btn-retry-failed').addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Retry tất cả jobs error & cancelled?' }))) return;
    try {
      const res = await api('/api/jobs/retry-failed', { method: 'POST' });
      console.log('retry-failed:', res.retried);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  dom.modeSelect.addEventListener('change', async () => {
    const tabId = _activeTabId;
    const cfg = MODE_TAB_CONFIG[tabId];
    if (!cfg) return; // tab không có mode (settings) — ignore
    const newMode = dom.modeSelect.value;
    Settings.save(`${tabId}.mode`, newMode, getAuthToken());

    // Reg có server-side concurrency config (POST /api/config). Session/UPI
    // sync max_concurrent ngay tại lúc Run trong session.js / upi.js, không
    // cần round-trip thừa ở đây.
    if (tabId === 'reg') {
      state.mode = newMode;
      const target = _modeToConcurrency(newMode);
      try {
        await api('/api/config', {
          method: 'POST',
          body: JSON.stringify({ max_concurrent: target }),
        });
        state.maxConcurrent = target;
      } catch (err) {
        console.error(err);
      }
    }
  });

  dom.headlessToggle.addEventListener('change', async () => {
    const headless = dom.headlessToggle.checked;
    // Cảnh báo: jobs đang RUNNING không bị ảnh hưởng (browser đã launch)
    let runningCount = 0;
    for (const [, j] of state.jobs) {
      if (j.status === 'running') runningCount += 1;
    }
    if (runningCount > 0) {
      const ok = await Dialog.confirm({ message:
        `Có ${runningCount} job đang RUNNING — đổi Headless không ` +
        `áp dụng cho job đó (browser đã launch). Chỉ ảnh hưởng job mới.\n\n` +
        `Tiếp tục đổi sang ${headless ? 'Headless' : 'Headed'}?`
      });
      if (!ok) {
        dom.headlessToggle.checked = state.headless;
        return;
      }
    }
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ headless }),
      });
      state.headless = headless;
    } catch (err) {
      console.error(err);
      dom.headlessToggle.checked = state.headless;
    }
  });

  dom.debugToggle.addEventListener('change', async () => {
    const debug = dom.debugToggle.checked;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ debug }),
      });
      state.debug = debug;
    } catch (err) {
      console.error(err);
      dom.debugToggle.checked = state.debug;
    }
  });

  dom.proxyToggle.addEventListener('change', async () => {
    const useProxy = dom.proxyToggle.checked;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ use_proxy: useProxy }),
      });
      state.useProxy = useProxy;
    } catch (err) {
      console.error(err);
      dom.proxyToggle.checked = state.useProxy;
    }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 30 || val > 600) return;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) {
      console.error(err);
    }
  });

  dom.autoRetryMax.addEventListener('change', async () => {
    const val = parseInt(dom.autoRetryMax.value, 10);
    if (isNaN(val) || val < 0 || val > 10) return;
    const enabled = val > 0;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ auto_retry: enabled, auto_retry_max: val || 1 }),
      });
    } catch (err) {
      console.error(err);
    }
  });

  // Password field persist — write-through via Settings API (ui-only, no dedicated endpoint)
  dom.defaultPassword.addEventListener('input', () => {
    const token = getAuthToken();
    if (token) Settings.save('reg.default_password', dom.defaultPassword.value || null, token);
  });

  // ── Copy buttons ─────────────────────────────────────────────────
  dom.btnCopySuccess.addEventListener('click', () => copyText(dom.successPane.textContent));
  dom.btnCopyError.addEventListener('click', () => copyText(dom.errorPane.textContent));

  // ── SSE event stream ─────────────────────────────────────────────
  function applySnapshot(jobs) {
    state.order = jobs.map((j) => j.id);
    state.jobs.clear();
    for (const j of jobs) state.jobs.set(j.id, j);
    // Prune secretsCache theo job set hiện tại
    for (const cachedId of Array.from(secretsCache.keys())) {
      if (!state.jobs.has(cachedId)) secretsCache.delete(cachedId);
    }
    renderJobs();
    renderOutputs();
    scheduleSecretsRefresh();
  }

  function applyJobUpdate(j) {
    const prev = state.jobs.get(j.id);
    if (!prev) {
      state.order.push(j.id);
    }
    state.jobs.set(j.id, j);
    renderJobs();
    renderOutputs();
    // Khi job chuyển success/error → có thể có secrets mới → fetch lại
    if (j.status === 'success' || j.status === 'error') {
      scheduleSecretsRefresh();
    }
    if (j.status === 'error' && (!prev || prev.status !== 'error')) {
      playErrorAlert();
    }
    if (state.activeJobId === j.id) {
      // refresh log nếu đang xem
      renderLog(j.id);
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    secretsCache.delete(jobId);
    if (state.activeJobId === jobId) {
      state.activeJobId = null;
      renderLog(null);
    }
    renderJobs();
    renderOutputs();
  }

  function applyLog(jobId, line) {
    if (state.activeJobId !== jobId) return;
    const cls = /(error|FAILED|fatal)/i.test(line) ? 'log-line-error' : 'log-line-info';
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = line + '\n';
    dom.logPane.appendChild(span);
    dom.logPane.scrollTop = dom.logPane.scrollHeight;
  }

  // ── SseBus handler for 'reg' channel ───────────────────────────────
  SseBus.on('reg', (data) => {
    if (data.type === 'snapshot') {
      state.maxConcurrent = data.max_concurrent;
      if (typeof data.headless === 'boolean') {
        state.headless = data.headless;
        dom.headlessToggle.checked = data.headless;
      }
      if (typeof data.debug === 'boolean') {
        state.debug = data.debug;
        dom.debugToggle.checked = data.debug;
      }
      if (typeof data.use_proxy === 'boolean') {
        state.useProxy = data.use_proxy;
        dom.proxyToggle.checked = data.use_proxy;
      }
      if (data.job_timeout) {
        dom.jobTimeout.value = data.job_timeout;
      }
      applySnapshot(data.jobs);
    } else if (data.type === 'job') {
      applyJobUpdate(data.job);
    } else if (data.type === 'remove') {
      applyRemove(data.job_id);
    } else if (data.type === 'clear_finished') {
      api('/api/jobs').then((r) => applySnapshot(r.jobs)).catch(console.error);
    } else if (data.type === 'clear_all') {
      state.jobs.clear();
      state.order = [];
      secretsCache.clear();
      state.activeJobId = null;
      renderJobs();
      renderOutputs();
      renderLog(null);
    } else if (data.type === 'log') {
      applyLog(data.job_id, data.line);
    }
  });

  // ── Mail Mode ─────────────────────────────────────────────────────
  let _workerConfigDebounce = null;

  function getWorkerConfig() {
    // Hydrate from Settings store (mail_mode.worker_config is a JSON object)
    const cfg = Settings.get('mail_mode.worker_config');
    return (cfg && typeof cfg === 'object') ? cfg : {};
  }

  function saveWorkerConfig(cfg) {
    Settings.save('mail_mode.worker_config', cfg, getAuthToken());
  }

  function renderMailModeSelector(modes) {
    dom.mailModeSelect.innerHTML = modes.map(m =>
      `<option value="${escHtml(m.id)}">${escHtml(m.label)}</option>`
    ).join('');
  }

  function renderMailModeConfig(modes, modeId) {
    const spec = modes.find(m => m.id === modeId);
    if (!spec || spec.config_schema.length === 0) {
      dom.mailModeConfigHost.innerHTML = '';
      return;
    }
    const saved = getWorkerConfig();
    // Ensure defaults are persisted immediately
    let needSave = false;
    for (const f of spec.config_schema) {
      if (saved[f.key] === undefined) {
        saved[f.key] = f.default;
        needSave = true;
      }
    }
    if (needSave) saveWorkerConfig(saved);
    const fields = spec.config_schema.map(f => {
      const val = saved[f.key] !== undefined ? saved[f.key] : f.default;
      const widthClass = f.key === 'api_key' ? 'config-field-short' : 'config-field-long';
      return `
        <label class="input-group ${widthClass}">
          <span class="input-label">${escHtml(f.label)}${f.required ? ' *' : ''}</span>
          <input type="text" data-config-key="${escHtml(f.key)}" value="${escHtml(val)}" spellcheck="false" autocomplete="off" />
          <span class="input-error" id="err-${escHtml(f.key)}"></span>
        </label>
      `;
    }).join('');
    // Sử dụng display:contents wrapper — elements trực tiếp nằm trong flex row
    dom.mailModeConfigHost.innerHTML = `<div class="mail-mode-config-panel">${fields}</div>`;
    // Attach events
    dom.mailModeConfigHost.querySelectorAll('input[data-config-key]').forEach(inp => {
      inp.addEventListener('input', () => debouncePersistWorkerConfig());
      inp.addEventListener('blur', () => debouncePersistWorkerConfig());
    });
    validateWorkerConfig();
  }

  function debouncePersistWorkerConfig() {
    clearTimeout(_workerConfigDebounce);
    _workerConfigDebounce = setTimeout(() => {
      const cfg = {};
      dom.mailModeConfigHost.querySelectorAll('input[data-config-key]').forEach(inp => {
        cfg[inp.dataset.configKey] = inp.value;
      });
      saveWorkerConfig(cfg);
      validateWorkerConfig();
    }, 500);
  }

  function validateWorkerConfig() {
    if (state.currentMailMode !== 'worker') {
      dom.btnRun.disabled = false;
      return;
    }
    const spec = state.mailModes.find(m => m.id === 'worker');
    if (!spec) return;
    let valid = true;
    for (const f of spec.config_schema) {
      const inp = dom.mailModeConfigHost.querySelector(`input[data-config-key="${f.key}"]`);
      const errEl = document.getElementById(`err-${f.key}`);
      if (!inp || !errEl) continue;
      const val = inp.value.trim();
      if (f.validate_prefix && f.validate_prefix.length) {
        if (!f.validate_prefix.some(p => val.startsWith(p))) {
          errEl.textContent = `Must start with ${f.validate_prefix.join(' or ')}`;
          errEl.className = 'input-error';
          valid = false;
          continue;
        }
      }
      if (f.required && !val) {
        errEl.textContent = 'Required';
        errEl.className = 'input-error';
        valid = false;
        continue;
      }
      if (!f.required && !val) {
        errEl.textContent = 'Blank - Worker sends no Authorization header';
        errEl.className = 'input-warn';
        continue;
      }
      errEl.textContent = '';
    }
    dom.btnRun.disabled = !valid;
  }

  function applyMailMode(modeId) {
    state.currentMailMode = modeId;
    dom.mailModeSelect.value = modeId;
    Settings.save('mail_mode.current', modeId, getAuthToken());
    const spec = state.mailModes.find(m => m.id === modeId);
    if (spec) {
      const uiCopy = mailModeUiCopy[modeId] || {};
      dom.comboInput.placeholder = uiCopy.input_placeholder || spec.input_placeholder;
      dom.inputHint.textContent = uiCopy.input_help || spec.input_help;
    }
    renderMailModeConfig(state.mailModes, modeId);
  }

  async function bootstrapMailModes() {
    try {
      const data = await api('/api/mail-modes');
      state.mailModes = data.modes || [];
    } catch (err) {
      console.error('Failed to load mail modes:', err);
      state.mailModes = [
        { id: 'outlook', label: 'Hotmail (combo)', input_placeholder: 'email|password|refresh_token|client_id', input_help: 'One Outlook combo per line.', config_schema: [] },
      ];
    }
    renderMailModeSelector(state.mailModes);
    // Restore from Settings store (DB-backed)
    const saved = Settings.get('mail_mode.current');
    const validIds = state.mailModes.map(m => m.id);
    const initial = (saved && validIds.includes(saved)) ? saved : 'icloud_v3';
    applyMailMode(initial);
    // Listen change
    dom.mailModeSelect.addEventListener('change', () => {
      applyMailMode(dom.mailModeSelect.value);
    });

    // ── Reg Mode selector (browser / hybrid) ─────
    const savedRegMode = Settings.get('reg_mode.current');
    if (savedRegMode && ['browser', 'hybrid'].includes(savedRegMode)) {
      dom.regModeSelect.value = savedRegMode;
    }
    dom.regModeSelect.addEventListener('change', () => {
      Settings.save('reg_mode.current', dom.regModeSelect.value, getAuthToken());
    });
  }

  // ── Init ─────────────────────────────────────────────────────────
  // Settings hydration: load all settings from DB via Settings.bootstrap(token),
  // then hydrate UI controls. Server is source of truth (write-through from
  // POST /api/config ensures DB stays in sync).

  // Restore combo textarea — chỉ mất khi user bấm Clear Input
  const _savedReg = localStorage.getItem(LS_INPUT_REG);
  if (_savedReg) dom.comboInput.value = _savedReg;

  // Bootstrap: load settings from DB then hydrate UI
  (async () => {
    const token = getAuthToken();
    await Settings.bootstrap(token);

    // Hydrate state + UI controls từ Settings store (DB-backed).
    // Mode select KHÔNG hydrate ở đây — sẽ apply qua `_applyTabMode` khi
    // initTabs() chạy (event 'gpt:tab' → load `<tab>.mode` per-tab).
    const headless = Settings.get('reg.headless');
    if (typeof headless === 'boolean') state.headless = headless;
    dom.headlessToggle.checked = state.headless;

    const debug = Settings.get('reg.debug');
    if (typeof debug === 'boolean') state.debug = debug;
    dom.debugToggle.checked = state.debug;

    const useProxy = Settings.get('reg.use_proxy');
    if (typeof useProxy === 'boolean') state.useProxy = useProxy;
    dom.proxyToggle.checked = state.useProxy;

    const defaultPassword = Settings.get('reg.default_password');
    if (defaultPassword) dom.defaultPassword.value = defaultPassword;

    const jobTimeout = Settings.get('reg.job_timeout');
    if (typeof jobTimeout === 'number') dom.jobTimeout.value = jobTimeout;

    const autoRetry = Settings.get('reg.auto_retry');
    const autoRetryMax = Settings.get('reg.auto_retry_max');
    if (typeof autoRetryMax === 'number') {
      dom.autoRetryMax.value = autoRetry ? autoRetryMax : 0;
    }

    // Server GET /api/config — source of truth cho runtime state (headless/debug/etc.)
    // Override từ DB nếu server đã apply khác (ví dụ manager changed in-memory).
    try {
      const cfg = await api('/api/config');
      if (typeof cfg.headless === 'boolean') {
        state.headless = cfg.headless;
        dom.headlessToggle.checked = cfg.headless;
      }
      if (typeof cfg.debug === 'boolean') {
        state.debug = cfg.debug;
        dom.debugToggle.checked = cfg.debug;
      }
      if (typeof cfg.use_proxy === 'boolean') {
        state.useProxy = cfg.use_proxy;
        dom.proxyToggle.checked = cfg.use_proxy;
      }
      if (typeof cfg.job_timeout === 'number') {
        dom.jobTimeout.value = cfg.job_timeout;
      }
      if (typeof cfg.auto_retry_max === 'number') {
        dom.autoRetryMax.value = cfg.auto_retry ? cfg.auto_retry_max : 0;
      }
    } catch (err) {
      console.error('GET /api/config failed, dùng Settings DB fallback:', err);
    }

    // initTabs + bootstrapMailModes phải chạy SAU Settings.bootstrap()
    // vì cần Settings.get('ui.active_tab') + Settings.get('mail_mode.current')
    initTabs();
    bootstrapMailModes();
  })();

  updateComboCount();

  // Start unified SSE connection (single connection for all channels)
  SseBus.connect();

  // Timer cập nhật duration cho jobs đang running mỗi giây
  setInterval(() => {
    let hasRunning = false;
    for (const [id, j] of state.jobs) {
      if (j.status === 'running' && j.started_at) {
        hasRunning = true;
        j.duration = (Date.now() / 1000) - j.started_at;
      }
    }
    if (hasRunning) renderJobs();
  }, 1000);
})();
