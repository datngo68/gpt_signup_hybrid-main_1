/* gpt_signup_hybrid — Get Link tab logic (3 modes: combo, session_json, access_token) */
(() => {
  'use strict';

  // ── LocalStorage keys ─────────────────────────────────────────────
  // Mỗi mode dùng 1 key riêng để chuyển tab không bị mất context.
  const LS_LINK_INPUT_PREFIX = 'gpt_reg.link.input.'; // + mode

  const MODE_CONFIG = {
    combo: {
      placeholder: 'email@hotmail.com|password123|DNPARKKMM5EYOPDG...\nemail2@outlook.com|pass456|I77PEBZQNEBE67SU...',
    },
    session_json: {
      placeholder: '{\n  "accessToken": "eyJhbGci...",\n  "user": { "email": "user@example.com", ... },\n  ...\n}',
    },
    access_token: {
      placeholder: 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ...\neyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ...',
    },
  };

  const state = {
    jobs: new Map(),
    order: [],
    activeJobId: null,
    maxConcurrent: 1,
    mode: 'combo',
    region: 'VN',
  };

  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput: $('link-combo-input'),
    btnRun: $('link-btn-run'),
    btnStopAll: $('link-btn-stop-all'),
    btnClearInput: $('link-btn-clear-input'),
    btnClearDone: $('link-btn-clear-done'),
    btnCopyError: $('link-btn-copy-error'),
    comboCount: $('link-combo-count'),
    jobTimeout: $('link-job-timeout'),
    regionSelect: $('link-region-select'),
    jobList: $('link-job-list'),
    jobSummary: $('link-job-summary'),
    logPane: $('link-log-pane'),
    logTarget: $('link-log-target'),
    errorPane: $('link-error-pane'),
  };

  // ─── Mode switching ───
  function inputKey(mode) { return LS_LINK_INPUT_PREFIX + mode; }
  function persistCurrentInput() {
    window.GptUi.persistTextarea(inputKey(state.mode), dom.comboInput.value);
  }
  let _modeInitialized = false;
  function applyMode(mode) {
    // Lần init đầu: textarea còn trống, không persist để khỏi ghi đè key
    // đã lưu trước đó. Sau khi initialized, mỗi lần switch mode đều persist
    // input của mode cũ trước khi đổi sang mode mới.
    if (_modeInitialized && state.mode !== mode) {
      persistCurrentInput();
    }
    state.mode = mode;
    modeBtns.forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));
    const cfg = MODE_CONFIG[mode];
    dom.comboInput.placeholder = cfg.placeholder;
    const saved = localStorage.getItem(inputKey(mode));
    dom.comboInput.value = saved || '';
    Settings.save('ui.link_mode', mode, window.GptUi.getAuthToken());
    updateComboCount();
    _modeInitialized = true;
  }

  const modeBtns = document.querySelectorAll('.link-mode-btn');
  modeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      applyMode(btn.dataset.mode);
    });
  });

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function fmtDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return secs.toFixed(1) + 's';
    return Math.floor(secs / 60) + 'm' + Math.floor(secs % 60) + 's';
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

  function updateComboCount() {
    const text = dom.comboInput.value.trim();
    let count = 0;

    if (state.mode === 'combo' || state.mode === 'access_token') {
      count = text.split('\n').filter((line) => {
        const trimmed = line.trim();
        return trimmed && !trimmed.startsWith('#');
      }).length;
    } else if (state.mode === 'session_json') {
      // Single JSON only — valid or not
      count = text.length > 0 ? 1 : 0;
    }

    const label = state.mode === 'session_json' ? 'session' : 'item';
    dom.comboCount.textContent = `${count} ${label}${count === 1 ? '' : 's'}`;
  }

  function renderErrors() {
    const errors = state.order
      .map((id) => state.jobs.get(id))
      .filter((job) => job && job.status === 'error')
      .map((job) => `${job.email}  →  ${job.error || 'unknown'}`);

    dom.errorPane.textContent = errors.length ? errors.join('\n') : 'No errors yet.';
  }

  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">Paste input and click Get Link.</div>';
      dom.jobSummary.textContent = '0 total';
      renderErrors();
      return;
    }

    const stats = { queued: 0, running: 0, success: 0, error: 0, cancelled: 0 };
    const html = state.order.map((id, idx) => {
      const job = state.jobs.get(id);
      if (!job) return '';

      stats[job.status] = (stats[job.status] || 0) + 1;
      const cls = state.activeJobId === id ? 'job is-active' : 'job';

      const actions = [];
      if (job.payment_link) {
        actions.push(
          `<button class="icon-btn" data-action="copy-link" data-id="${escHtml(id)}" title="Copy payment link">${window.GptUi.icon('link')}</button>`,
        );
      }
      // Indonesia (ID) region: nút GoPay/Midtrans (compact, no emoji)
      if (job.region === 'ID' && job.status === 'success' && job.payment_link) {
        actions.push(
          `<button class="btn-gopay btn-gopay-session" data-action="copy-session" data-id="${escHtml(id)}" title="Copy session JSON">Session</button>`,
        );
        actions.push(
          `<button class="btn-gopay btn-gopay-copy" data-action="get-gopay" data-id="${escHtml(id)}" title="Copy GoPay/Midtrans link">GoPay</button>`,
        );
        actions.push(
          `<button class="btn-gopay btn-gopay-refresh" data-action="refresh-gopay" data-id="${escHtml(id)}" title="Lấy lại link GoPay mới">Refresh</button>`,
        );
      }
      if (job.status === 'running') {
        actions.push(
          `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${window.GptUi.icon('stop')}</button>`,
        );
      } else {
        // Cho phép retry từ mọi terminal status (success/error/cancelled).
        // Dùng region gốc của job (snapshot lúc add_jobs).
        actions.push(
          `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry (region=${escHtml(job.region || '?')})">${window.GptUi.icon('retry')}</button>`,
        );
      }
      actions.push(
        `<button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>`,
      );

      const meta = job.payment_link
        ? `<div class="job-meta" title="${escHtml(job.payment_link)}">${escHtml(job.payment_link)}</div>`
        : '';

      const modeTag = job.mode && job.mode !== 'combo' ? `<span class="muted">[${escHtml(job.mode)}]</span> ` : '';
      const regionTag = job.region ? `<span class="muted">[${escHtml(job.region)}]</span> ` : '';

      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(job.status)}">${escHtml(job.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(job.email)}">${regionTag}${modeTag}${escHtml(job.email)}</div>
            ${meta}
          </div>
          <div class="job-duration">${escHtml(fmtDuration(job.duration))}</div>
          <div class="job-actions">${actions.join('')}</div>
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
    renderErrors();
  }

  function highlightInputLine(jobId) {
    const job = state.jobs.get(jobId);
    if (!job || !job.email) return;

    const text = dom.comboInput.value;
    if (!text) return;

    const lines = text.split('\n');
    const email = job.email.toLowerCase();
    let offset = 0;
    let found = false;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.toLowerCase().includes(email)) {
        const start = offset;
        const end = offset + line.length;
        dom.comboInput.focus();
        dom.comboInput.setSelectionRange(start, end);
        // Scroll textarea to show the highlighted line
        const lineHeight = parseInt(getComputedStyle(dom.comboInput).lineHeight, 10) || 18;
        dom.comboInput.scrollTop = Math.max(0, i * lineHeight - dom.comboInput.clientHeight / 2);
        found = true;
        break;
      }
      offset += line.length + 1; // +1 for \n
    }

    if (!found) {
      dom.comboInput.setSelectionRange(0, 0);
    }
  }

  function renderLog(jobId) {
    if (!jobId) {
      dom.logPane.textContent = '';
      dom.logTarget.textContent = '-';
      return;
    }

    const job = state.jobs.get(jobId);
    if (!job) return;

    dom.logTarget.textContent = job.email;
    api(`/api/link/jobs/${jobId}`).then((data) => {
      dom.logPane.textContent = (data.log_lines || []).join('\n');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  function applySnapshot(jobs) {
    state.order = jobs.map((job) => job.id);
    state.jobs.clear();
    jobs.forEach((job) => state.jobs.set(job.id, job));
    renderJobs();
    if (state.activeJobId && !state.jobs.has(state.activeJobId)) {
      state.activeJobId = null;
      renderLog(null);
    }
  }

  function applyJobUpdate(job) {
    const prev = state.jobs.get(job.id);
    if (!prev) state.order.push(job.id);
    state.jobs.set(job.id, job);
    renderJobs();
    if (state.activeJobId === job.id) renderLog(job.id);
    if (job.status === 'error' && (!prev || prev.status !== 'error') && window.GptUi?.playErrorAlert) {
      window.GptUi.playErrorAlert();
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    if (state.activeJobId === jobId) {
      state.activeJobId = null;
      renderLog(null);
    }
    renderJobs();
  }

  function applyLog(jobId, line) {
    if (state.activeJobId !== jobId) return;
    dom.logPane.textContent += `${line}\n`;
    dom.logPane.scrollTop = dom.logPane.scrollHeight;
  }

  // ── SSE via unified SseBus ──────────────────────────────────────
  SseBus.on('link', (data) => {
    if (data.type === 'snapshot') {
      state.maxConcurrent = data.max_concurrent || state.maxConcurrent;
      if (data.job_timeout) dom.jobTimeout.value = data.job_timeout;
      if (data.region) {
        state.region = data.region;
        dom.regionSelect.value = data.region;
      }
      applySnapshot(data.jobs || []);
    } else if (data.type === 'job') {
      applyJobUpdate(data.job);
    } else if (data.type === 'log') {
      applyLog(data.job_id, data.line);
    } else if (data.type === 'remove') {
      applyRemove(data.job_id);
    } else if (data.type === 'clear_finished') {
      api('/api/link/jobs').then((response) => applySnapshot(response.jobs || [])).catch(console.error);
    }
  });

  dom.jobList.addEventListener('click', (event) => {
    const actionBtn = event.target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      event.stopPropagation();

      if (action === 'copy-link') {
        const job = state.jobs.get(id);
        if (job && job.payment_link) {
          window.GptUi.copyText(job.payment_link);
          // Auto-download session khi copy link
          api(`/api/link/jobs/${id}`).then((data) => {
            if (data.access_token) {
              const sessionJson = {
                user: { email: data.email || '' },
                accessToken: data.access_token || '',
              };
              const jsonStr = JSON.stringify(sessionJson, null, 2);
              const blob = new Blob([jsonStr], { type: 'application/json' });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = `session_${data.email || id}.json`;
              a.click();
              URL.revokeObjectURL(url);
            }
          }).catch(() => {});
        }
      } else if (action === 'copy-session') {
        // Copy + download session JSON
        const btn = actionBtn;
        btn.disabled = true;
        btn.textContent = '...';
        api(`/api/link/jobs/${id}`).then((data) => {
          const sessionJson = {
            user: { email: data.email || '' },
            accessToken: data.access_token || '',
          };
          const jsonStr = JSON.stringify(sessionJson, null, 2);
          // Copy to clipboard
          window.GptUi.copyText(jsonStr);
          // Download file
          const blob = new Blob([jsonStr], { type: 'application/json' });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `session_${data.email || id}.json`;
          a.click();
          URL.revokeObjectURL(url);
          btn.textContent = 'Copied';
          setTimeout(() => { btn.textContent = 'Session'; btn.disabled = false; }, 1500);
        }).catch((err) => {
          btn.textContent = 'Error';
          setTimeout(() => { btn.textContent = 'Session'; btn.disabled = false; }, 2000);
          console.error(err);
        });
      } else if (action === 'get-gopay') {
        // Lấy GoPay/Midtrans link từ payment_link hiện tại
        const btn = actionBtn;
        btn.disabled = true;
        btn.textContent = '...';
        api(`/api/link/jobs/${id}/gopay-link`, { method: 'POST' }).then((data) => {
          if (data.gopay_link) {
            window.GptUi.copyText(data.gopay_link);
            btn.textContent = 'Copied';
          } else {
            btn.textContent = 'No link';
          }
          setTimeout(() => { btn.textContent = 'GoPay'; btn.disabled = false; }, 2000);
        }).catch(async (err) => {
          btn.textContent = 'Failed';
          setTimeout(() => { btn.textContent = 'GoPay'; btn.disabled = false; }, 2000);
          await Dialog.alert({ message: 'GoPay link error: ' + err.message });
        });
      } else if (action === 'refresh-gopay') {
        // Chạy lại: lấy Stripe link mới → rồi lấy Midtrans link
        const btn = actionBtn;
        btn.disabled = true;
        btn.textContent = '...';
        api(`/api/link/jobs/${id}/refresh-gopay-link`, { method: 'POST' }).then((data) => {
          if (data.gopay_link) {
            window.GptUi.copyText(data.gopay_link);
            btn.textContent = 'Copied';
          } else {
            btn.textContent = 'No link';
          }
          setTimeout(() => { btn.textContent = 'Refresh'; btn.disabled = false; }, 2000);
        }).catch(async (err) => {
          btn.textContent = 'Failed';
          setTimeout(() => { btn.textContent = 'Refresh'; btn.disabled = false; }, 2000);
          await Dialog.alert({ message: 'Refresh GoPay error: ' + err.message });
        });
      } else if (action === 'retry') {
        // 3B: retry dùng region GỐC của job (snapshot lúc add_jobs).
        api(`/api/link/jobs/${id}/retry`, {
          method: 'POST',
          body: JSON.stringify({}),
        }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'stop' || action === 'remove') {
        api(`/api/link/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      }
      return;
    }

    const row = event.target.closest('.job');
    if (!row) return;
    state.activeJobId = row.dataset.id;
    renderJobs();
    renderLog(state.activeJobId);
    highlightInputLine(state.activeJobId);
  });

  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) {
      await Dialog.alert({ message: 'Paste input first.' });
      return;
    }

    dom.btnRun.disabled = true;
    try {
      await api('/api/link/jobs', {
        method: 'POST',
        body: JSON.stringify({ combos, mode: state.mode, region: state.region, reg_mode: document.getElementById('reg-mode-select')?.value || 'browser' }),
      });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
    }
  });

  dom.btnStopAll.addEventListener('click', () => {
    api('/api/link/jobs/stop-all', { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    window.GptUi.clearPersistedTextarea(inputKey(state.mode));
  });

  dom.btnClearDone.addEventListener('click', () => {
    api('/api/link/jobs/clear-finished', { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
  });

  dom.btnCopyError.addEventListener('click', () => {
    window.GptUi.copyText(dom.errorPane.textContent);
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 30 || val > 600) return;
    try {
      await api('/api/link/config', {
        method: 'POST',
        body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) { console.error(err); }
  });

  dom.regionSelect.addEventListener('change', async () => {
    state.region = dom.regionSelect.value;
    try {
      await api('/api/link/config', {
        method: 'POST',
        body: JSON.stringify({ region: state.region }),
      });
    } catch (err) { console.error(err); }
  });

  dom.comboInput.addEventListener('input', () => {
    updateComboCount();
    persistCurrentInput();
  });
  // Restore mode + input đã lưu (ưu tiên Settings cache, fallback button .active)
  const _savedMode = Settings.get('ui.link_mode');
  const _validModes = Object.keys(MODE_CONFIG);
  const _initialMode = _validModes.includes(_savedMode)
    ? _savedMode
    : (document.querySelector('.link-mode-btn.active')?.dataset.mode || 'combo');
  applyMode(_initialMode);

  setInterval(() => {
    let hasRunning = false;
    for (const [, job] of state.jobs) {
      if (job.status === 'running' && job.started_at) {
        hasRunning = true;
        job.duration = (Date.now() / 1000) - job.started_at;
      }
    }
    if (hasRunning) renderJobs();
  }, 1000);
})();
