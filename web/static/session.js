/* gpt_signup_hybrid — Get Session tab logic */
(() => {
  'use strict';

  // ── LocalStorage keys ─────────────────────────────────────────────
  const LS_INPUT_SES = 'gpt_reg.input.session';

  // ── State ─────────────────────────────────────────────────────────
  const state = {
    jobs: new Map(),
    order: [],
    activeJobId: null,
    maxConcurrent: 1,
  };

  // ── DOM refs ──────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput:   $('ses-combo-input'),
    btnRun:       $('ses-btn-run'),
    btnStopAll:   $('ses-btn-stop-all'),
    btnClearInput: $('ses-btn-clear-input'),
    comboCount:   $('ses-combo-count'),
    jobTimeout:   $('ses-job-timeout'),
    jobList:      $('ses-job-list'),
    jobSummary:   $('ses-job-summary'),
    logPane:      $('ses-log-pane'),
    logTarget:    $('ses-log-target'),
    freePane:     $('ses-free-pane'),
    plusPane:     $('ses-plus-pane'),
    freeCount:    $('ses-free-count'),
    plusCount:    $('ses-plus-count'),
    btnCopyFree:  $('ses-btn-copy-free'),
    btnCopyPlus:  $('ses-btn-copy-plus'),
    errorPane:    $('ses-error-pane'),
    btnCopyError: $('ses-btn-copy-error'),
    btnClearDone: $('ses-btn-clear-done'),
  };

  // ── Helpers ───────────────────────────────────────────────────────
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
    const token = window.GptUi.getAuthToken();
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
    window.GptUi.persistTextarea(LS_INPUT_SES, dom.comboInput.value);
  });

  // ── Render job list ───────────────────────────────────────────────
  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">Paste combos and click Get Session.</div>';
      dom.jobSummary.textContent = '0 total';
      return;
    }

    const stats = { queued: 0, running: 0, success: 0, error: 0, cancelled: 0 };
    const html = state.order.map((id, idx) => {
      const j = state.jobs.get(id);
      if (!j) return '';
      stats[j.status] = (stats[j.status] || 0) + 1;
      const cls = state.activeJobId === id ? 'job is-active' : 'job';

      let actionBtns = '';
      if (j.status === 'running') {
        actionBtns = `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${window.GptUi.icon('stop')}</button>`;
      } else if (j.status === 'success') {
        actionBtns = `
          <button class="icon-btn" data-action="reload" data-id="${escHtml(id)}" title="Reload session (lấy lại để xem type account mới)">${window.GptUi.icon('retry')}</button>
          <button class="icon-btn" data-action="download" data-id="${escHtml(id)}" title="Download JSON">${window.GptUi.icon('download')}</button>
          <button class="icon-btn" data-action="copy-json" data-id="${escHtml(id)}" title="Copy JSON">${window.GptUi.icon('copy')}</button>
          <button class="icon-btn" data-action="copy-token" data-id="${escHtml(id)}" title="Copy access token">${window.GptUi.icon('token')}</button>
        `;
      } else {
        actionBtns = `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${window.GptUi.icon('retry')}</button>`;
      }

      const planBadge = j.plan_type
        ? `<span class="plan-badge plan-${escHtml(j.plan_type.toLowerCase())}">${escHtml(j.plan_type)}</span>`
        : '';

      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(j.status)}">${escHtml(j.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(j.email)}">
              <span class="job-email-text">${escHtml(j.email)}</span>
              ${planBadge}
            </div>
          </div>
          <div class="job-duration">${escHtml(fmtDuration(j.duration))}</div>
          <div class="job-actions">
            ${actionBtns}
            <button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>
          </div>
        </div>
      `;
    }).join('');

    dom.jobList.innerHTML = html;
    dom.jobSummary.textContent = [
      `${state.order.length} total`,
      stats.running ? `${stats.running} running` : '',
      stats.success ? `${stats.success} done` : '',
      stats.error ? `${stats.error} failed` : '',
    ].filter(Boolean).join(' · ');
  }

  // ── Render outputs ────────────────────────────────────────────────
  // Session data lưu local khi job success (để copy/download)
  const sessionCache = new Map(); // job_id → session_data

  // Output panes: 2 blocks Free / Plus, format `email|password|secret`.
  //
  // 2 nguồn lookup secret theo thứ tự ưu tiên (giống pattern UPI tab):
  //   1. _pastedSecretsByEmail — parse trực tiếp từ textarea lúc user click
  //      Run; có NGAY, không phụ thuộc network. Persist localStorage để
  //      sống qua reload.
  //   2. secretsCache — fetch async từ /api/session/jobs/secrets (fallback
  //      khi input bị clear hoặc reload mất localStorage).
  // Backend KHÔNG đưa password/secret vào job.to_dict() (tránh leak qua
  // SSE snapshot) → frontend phải tự cache.
  const secretsCache = new Map(); // job_id → {email, password, secret}
  const _pastedSecretsByEmail = new Map(); // emailLower → {password, secret}
  let _secretsRefreshScheduled = false;

  const LS_PASTED_SECRETS_SES = 'gpt_reg.session.pasted_secrets';

  function _persistPastedSecrets() {
    try {
      const obj = {};
      for (const [k, v] of _pastedSecretsByEmail.entries()) obj[k] = v;
      localStorage.setItem(LS_PASTED_SECRETS_SES, JSON.stringify(obj));
    } catch (_) { /* quota — ignore */ }
  }

  function _loadPastedSecrets() {
    try {
      const raw = localStorage.getItem(LS_PASTED_SECRETS_SES);
      if (!raw) return;
      const obj = JSON.parse(raw);
      if (obj && typeof obj === 'object') {
        for (const [k, v] of Object.entries(obj)) _pastedSecretsByEmail.set(k, v);
      }
    } catch (_) { /* corrupt — ignore */ }
  }

  // Parse input textarea, populate _pastedSecretsByEmail. Gọi NGAY khi user
  // click Run (trước khi POST add_jobs) để cache có data trước khi SSE job
  // event đầu tiên về. KHÔNG validate format ở đây — backend đã validate.
  function _capturePastedSecrets(rawText) {
    const lines = rawText.split('\n');
    let added = 0;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const parts = trimmed.split('|').map((p) => p.trim());
      if (parts.length < 2 || !parts[0].includes('@')) continue;
      const email = parts[0].toLowerCase();
      _pastedSecretsByEmail.set(email, {
        password: parts[1] || '',
        secret: parts[2] || '',
      });
      added += 1;
    }
    if (added > 0) _persistPastedSecrets();
    return added;
  }

  function scheduleSecretsRefresh() {
    if (_secretsRefreshScheduled) return;
    _secretsRefreshScheduled = true;
    // Debounce ~150ms — gom nhiều SSE event vào 1 fetch.
    setTimeout(() => {
      _secretsRefreshScheduled = false;
      refreshSecrets();
    }, 150);
  }

  function refreshSecrets() {
    return api('/api/session/jobs/secrets').then((data) => {
      const map = data.secrets || {};
      // Replace toàn bộ cache (server là source of truth — job removed sẽ
      // biến mất khỏi map). Không merge vì stale entry sẽ leak qua copy.
      secretsCache.clear();
      for (const id of Object.keys(map)) {
        secretsCache.set(id, map[id] || {});
      }
      renderOutputs();
    }).catch((err) => {
      console.warn('[session] refreshSecrets failed:', err && err.message);
    });
  }

  function _resolveSecretsFor(j) {
    // Lookup password+secret cho 1 job, ưu tiên pasted map (chắc chắn có
    // ngay khi user click Run) → fallback secretsCache (fetch async).
    const id = j.id;
    const emailLow = (j.email || '').toLowerCase();
    const pasted = _pastedSecretsByEmail.get(emailLow);
    if (pasted && pasted.password) {
      return { password: pasted.password, secret: pasted.secret || '' };
    }
    const cached = secretsCache.get(id);
    if (cached && cached.password) {
      return { password: cached.password, secret: cached.secret || '' };
    }
    return { password: '', secret: '' };
  }

  // Phân loại plan: PLUS = bất kỳ paid plan (plus/pro/team/enterprise/business),
  // FREE = phần còn lại (free hoặc null/unknown). User yêu cầu chỉ 2 blocks
  // → gộp tất cả paid vào PLUS để không drop data.
  function _isPlusPlan(planType) {
    if (!planType) return false;
    const p = String(planType).toLowerCase().trim();
    return p !== '' && p !== 'free';
  }

  function _formatAccountLine(j) {
    const { password, secret } = _resolveSecretsFor(j);
    if (!password) {
      return `${j.email}  (đang tải secrets...)`;
    }
    return secret
      ? `${j.email}|${password}|${secret}`
      : `${j.email}|${password}`;
  }

  function renderOutputs() {
    const freeLines = [];
    const plusLines = [];
    const errorLines = [];
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j) continue;
      if (j.status === 'success') {
        // Auto-fetch full session data nếu chưa có (cho download/copy JSON).
        if (j.has_session && !sessionCache.has(id)) loadSessionData(id);
        if (_isPlusPlan(j.plan_type)) {
          plusLines.push(_formatAccountLine(j));
        } else {
          freeLines.push(_formatAccountLine(j));
        }
      } else if (j.status === 'error' && j.error) {
        errorLines.push(`${j.email}  →  ${j.error || 'unknown'}`);
      }
    }
    dom.freePane.textContent = freeLines.length
      ? freeLines.join('\n')
      : 'Format: email|password|secret_2fa';
    dom.plusPane.textContent = plusLines.length
      ? plusLines.join('\n')
      : 'Format: email|password|secret_2fa';
    dom.freeCount.textContent = freeLines.length;
    dom.plusCount.textContent = plusLines.length;
    dom.errorPane.textContent = errorLines.length
      ? errorLines.join('\n')
      : 'No errors yet.';
  }

  function loadSessionData(jobId) {
    api(`/api/session/jobs/${jobId}`).then((data) => {
      if (data.session_data) {
        sessionCache.set(jobId, data.session_data);
      }
    }).catch(() => {});
  }

  // ── Render log ────────────────────────────────────────────────────
  function renderLog(jobId) {
    if (!jobId) {
      dom.logPane.textContent = '';
      dom.logTarget.textContent = '-';
      return;
    }
    const j = state.jobs.get(jobId);
    if (!j) return;
    dom.logTarget.textContent = j.email;
    api(`/api/session/jobs/${jobId}/log`).then((data) => {
      const lines = data.log || [];
      // Mỗi span tự kết thúc bằng '\n' (giống applyLog) để SSE append sau
      // không bị dính vào span cuối.
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal)/i.test(l) ? 'log-line-error' : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}\n</span>`;
      }).join('');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── Highlight dòng input tương ứng với job đang chọn ──────────────
  function highlightInputLine(jobId) {
    const j = state.jobs.get(jobId);
    if (!j || !j.email) return;
    const text = dom.comboInput.value;
    if (!text) return;
    const lines = text.split('\n');
    const target = j.email.trim().toLowerCase();
    let offset = 0;
    let foundIndex = -1;
    let start = 0;
    let end = 0;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const email = line.trim().split('|')[0].trim().toLowerCase();
      if (email === target) {
        foundIndex = i;
        start = offset;
        end = offset + line.length;
        break;
      }
      offset += line.length + 1; // +1 cho ký tự '\n'
    }
    if (foundIndex === -1) return;
    dom.comboInput.focus();
    dom.comboInput.setSelectionRange(start, end);
    // Scroll dòng được chọn vào giữa khung textarea
    const cs = getComputedStyle(dom.comboInput);
    const lineHeight = parseFloat(cs.lineHeight) || 16;
    const padTop = parseFloat(cs.paddingTop) || 0;
    const targetTop = padTop + foundIndex * lineHeight;
    dom.comboInput.scrollTop = Math.max(0, targetTop - dom.comboInput.clientHeight / 2);
  }

  // ── Job actions ───────────────────────────────────────────────────
  dom.jobList.addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      e.stopPropagation();
      if (action === 'retry') {
        api(`/api/session/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'reload') {
        // Lấy lại session để cập nhật type account (chạy lại get_session)
        sessionCache.delete(id);
        api(`/api/session/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'stop') {
        api(`/api/session/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'remove') {
        api(`/api/session/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'download' || action === 'copy-json' || action === 'copy-token') {
        // Lấy session data
        const cached = sessionCache.get(id);
        if (cached) {
          doSessionAction(action, id, cached);
        } else {
          api(`/api/session/jobs/${id}`).then((data) => {
            if (data.session_data) {
              sessionCache.set(id, data.session_data);
              doSessionAction(action, id, data.session_data);
            }
          }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
        }
      }
      return;
    }
    const row = e.target.closest('.job');
    if (row) {
      state.activeJobId = row.dataset.id;
      renderJobs();
      renderLog(state.activeJobId);
      highlightInputLine(state.activeJobId);
    }
  });

  function doSessionAction(action, jobId, sessionData) {
    const j = state.jobs.get(jobId);
    const email = j ? j.email : 'session';
    if (action === 'download') {
      const filename = `session.${email}.json`;
      const blob = new Blob([JSON.stringify(sessionData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } else if (action === 'copy-json') {
      window.GptUi.copyText(JSON.stringify(sessionData, null, 2));
    } else if (action === 'copy-token') {
      if (sessionData.accessToken) {
        window.GptUi.copyText(sessionData.accessToken);
      }
    }
  }

  // ── Run button ────────────────────────────────────────────────────
  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) { await Dialog.alert({ message: 'Paste combos first.' }); return; }
    // Capture pasted secrets NGAY (trước khi POST) để Output panes có data
    // ngay khi SSE job event đầu tiên về — không phụ thuộc race condition
    // /api/session/jobs/secrets fetch.
    _capturePastedSecrets(combos);
    dom.btnRun.disabled = true;
    try {
      // Sync config
      const _modeMap = { single: 1, multi: 2, multi3: 3, multi5: 5, multi10: 10, multi20: 20, multi30: 30, multi50: 50, multi100: 100, multi200: 200 };
      const target = _modeMap[document.getElementById('mode').value] || 1;
      await api('/api/session/config', {
        method: 'POST',
        body: JSON.stringify({ max_concurrent: target }),
      });
      const regMode = document.getElementById('reg-mode-select')?.value || 'browser';
      await api('/api/session/jobs', {
        method: 'POST',
        body: JSON.stringify({ combos, reg_mode: regMode }),
      });
      scheduleSecretsRefresh();
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
    }
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    window.GptUi.clearPersistedTextarea(LS_INPUT_SES);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    try {
      await api('/api/session/jobs/stop-all', { method: 'POST' });
    } catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.btnClearDone.addEventListener('click', async () => {
    try {
      await api('/api/session/jobs/clear-finished', { method: 'POST' });
    } catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 30) return;
    try {
      await api('/api/session/config', {
        method: 'POST',
        body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) { console.error(err); }
  });

  // ── Copy buttons ──────────────────────────────────────────────────
  dom.btnCopyError.addEventListener('click', () => {
    window.GptUi.copyText(dom.errorPane.textContent);
  });
  dom.btnCopyFree.addEventListener('click', () => {
    window.GptUi.copyText(dom.freePane.textContent);
  });
  dom.btnCopyPlus.addEventListener('click', () => {
    window.GptUi.copyText(dom.plusPane.textContent);
  });

  // ── SSE (via SseBus) ────────────────────────────────────────────
  function applySnapshot(jobs) {
    state.order = jobs.map((j) => j.id);
    state.jobs.clear();
    for (const j of jobs) state.jobs.set(j.id, j);
    // Prune sessionCache: chỉ giữ entry cho jobs còn trong snapshot
    for (const cachedId of Array.from(sessionCache.keys())) {
      if (!state.jobs.has(cachedId)) sessionCache.delete(cachedId);
    }
    // Refresh secretsCache để có password/secret cho Output panes (snapshot
    // có thể chứa jobs từ DB recovery — không qua Run button capture).
    scheduleSecretsRefresh();
    renderJobs();
    renderOutputs();
  }

  function applyJobUpdate(j) {
    const prev = state.jobs.get(j.id);
    if (!prev) state.order.push(j.id);
    state.jobs.set(j.id, j);
    if (!prev) scheduleSecretsRefresh();
    renderJobs();
    renderOutputs();
    if (state.activeJobId === j.id) renderLog(j.id);
    if (j.status === 'error' && (!prev || prev.status !== 'error') && window.GptUi?.playErrorAlert) {
      window.GptUi.playErrorAlert();
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    sessionCache.delete(jobId);
    if (state.activeJobId === jobId) { state.activeJobId = null; renderLog(null); }
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

  SseBus.on('session', (data) => {
    if (data.type === 'snapshot') {
      state.maxConcurrent = data.max_concurrent;
      applySnapshot(data.jobs);
    } else if (data.type === 'job') {
      applyJobUpdate(data.job);
    } else if (data.type === 'remove') {
      applyRemove(data.job_id);
    } else if (data.type === 'clear_finished') {
      api('/api/session/jobs').then((r) => applySnapshot(r.jobs)).catch(console.error);
    } else if (data.type === 'log') {
      applyLog(data.job_id, data.line);
    }
  });

  // ── Init ──────────────────────────────────────────────────────────
  // Restore textarea — chỉ mất khi user bấm Clear Input
  const _savedSes = localStorage.getItem(LS_INPUT_SES);
  if (_savedSes) dom.comboInput.value = _savedSes;
  updateComboCount();

  // Load pasted secrets từ localStorage + capture lại từ textarea hiện có
  // (cover trường hợp localStorage bị clear nhưng textarea vẫn còn data).
  _loadPastedSecrets();
  if (dom.comboInput.value) _capturePastedSecrets(dom.comboInput.value);
  // Initial fetch — pickup secrets cho jobs đã restore từ DB.
  scheduleSecretsRefresh();

  // Duration timer
  setInterval(() => {
    let hasRunning = false;
    for (const [, j] of state.jobs) {
      if (j.status === 'running' && j.started_at) {
        hasRunning = true;
        j.duration = (Date.now() / 1000) - j.started_at;
      }
    }
    if (hasRunning) renderJobs();
  }, 1000);
})();
