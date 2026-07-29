/* gpt_signup_hybrid — UPI QR tab logic.
   Job item compact 1-row giống Get Session. Click icon QR → mở modal full-size. */
(() => {
  'use strict';

  const LS_INPUT_UPI = 'gpt_reg.input.upi';

  const state = {
    jobs: new Map(),
    order: [],
    activeJobId: null,
    maxConcurrent: 1,
    approveRetries: 500,
    approveRetryDelay: 5,
  };

  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput:    $('upi-combo-input'),
    btnRun:        $('upi-btn-run'),
    btnStopAll:    $('upi-btn-stop-all'),
    btnClearInput: $('upi-btn-clear-input'),
    comboCount:    $('upi-combo-count'),
    approveRetries:$('upi-approve-retries'),
    approveRetryDelay: $('upi-approve-retry-delay'),
    restartThreshold: $('upi-restart-threshold'),
    maxRestarts: $('upi-max-restarts'),
    maxOuterCycles: $('upi-max-outer-cycles'),
    reloginBlockStreak: $('upi-relogin-block-streak'),
    jobTimeout:    $('upi-job-timeout'),
    proxyFromStep: $('upi-proxy-from-step'),
    loginProxyUrl: $('upi-login-proxy-url'),
    notifyToggle:  $('upi-notify-toggle'),
    jobList:       $('upi-job-list'),
    jobSummary:    $('upi-job-summary'),
    logPane:       $('upi-log-pane'),
    logTarget:     $('upi-log-target'),
    successPane:   $('upi-success-pane'),
    errorPane:     $('upi-error-pane'),
    btnCopySuccess:$('upi-btn-copy-success'),
    btnCopyError:  $('upi-btn-copy-error'),
    btnClearDone:  $('upi-btn-clear-done'),
    btnClearAll:   $('upi-btn-clear-all'),
    btnClearCookies: $('upi-btn-clear-cookies'),
    btnRetryFailed:$('upi-btn-retry-failed'),
    btnRetryExpiredFree: $('upi-btn-retry-expired-free'),
    // Modal
    modal:         $('upi-qr-modal'),
    modalImg:      $('upi-qr-modal-img'),
    modalEmail:    $('upi-qr-modal-email'),
    modalAmount:   $('upi-qr-modal-amount'),
    modalSource:   $('upi-qr-modal-source'),
    modalCs:       $('upi-qr-modal-cs'),
    modalPayLink:  $('upi-qr-modal-paylink'),
    modalPayLinkEmpty: $('upi-qr-modal-paylink-empty'),
    modalCopyLink: $('upi-qr-modal-copy-link'),
    modalCountdown:$('upi-qr-modal-countdown'),
    modalExpVn:    $('upi-qr-modal-exp-vn'),
    modalExpIn:    $('upi-qr-modal-exp-in'),
    modalClose:    $('upi-qr-modal-close'),
    modalOk:       $('upi-qr-modal-ok'),
    modalCopyImg:  $('upi-qr-modal-copy-img'),
    modalOpen:     $('upi-qr-modal-open'),
  };

  // ── Helpers ───────────────────────────────────────────────────────
  function fmtDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return secs.toFixed(1) + 's';
    return Math.floor(secs / 60) + 'm' + Math.floor(secs % 60) + 's';
  }

  function fmtAmount(amount) {
    if (!amount) return '';
    return `₹${(amount / 100).toFixed(2)}`;
  }

  // Countdown tới expires_at (unix seconds). Trả {text, expired}.
  function fmtCountdown(expiresAt) {
    if (!expiresAt) return { text: '', expired: false };
    const remainMs = expiresAt * 1000 - Date.now();
    if (remainMs <= 0) return { text: 'Hết hạn', expired: true };
    const total = Math.floor(remainMs / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return { text: `${m}:${String(s).padStart(2, '0')}`, expired: false };
  }

  // ── Plan check (sau khi QR hết hạn) ─────────────────────────────────
  // Sau khi QR hết hạn → auto-poll POST check-session đến khi thấy Plus hoặc
  // hết lượt (upgrade UPI→Plus có thể chậm propagate). Mỗi lần nhận
  // {ok, plan, is_plus, expires, error}; server cache vào job.plan_check.
  // _planCheckInflight dedupe request đang bay; _planPollState quản poller/job.
  const _planCheckInflight = new Set();

  // Track jobs đã auto-trigger (open QR + copy + recheck) khi countdown hết.
  // Tránh trigger lặp mỗi giây (updateCountdowns chạy mỗi 1s).
  // Jobs đã expired lúc page load cũng được add vào set này (trong applySnapshot)
  // để không auto-trigger cho expired cũ.
  const _autoTriggeredOnExpiry = new Set();

  // Auto-poll: tối đa 6 lần check THẬT, mỗi lần cách ~20s tính TỪ lúc lần
  // trước hoàn tất (completion-driven, KHÔNG setInterval song song) — 1 check
  // worst-case ~40s nên timer cứng 20s sẽ chồng request.
  const PLAN_POLL_INTERVAL_MS = 20000;
  const PLAN_POLL_MAX = 6;
  // jobId → { count, timer }. Entry tồn tại = đang poll HOẶC đã xong (giữ entry
  // để updateCountdowns mỗi giây không spawn poller mới). Xóa khi job
  // removed / rời trạng thái success (xem _stopPlanPoll).
  const _planPollState = new Map();

  function renderPlanBadge(j) {
    if (!j || j.status !== 'success') return '';
    const pc = j.plan_check;
    if (!pc) return '';
    if (!pc.ok) {
      const errShort = (pc.error || 'check fail').slice(0, 80);
      return `<span class="badge upi-plan-badge upi-plan-err"
        title="${escHtml(errShort)}">PLAN ?</span>`;
    }
    const plan = (pc.plan || '').toString();
    if (pc.is_plus) {
      return `<span class="badge upi-plan-badge upi-plan-plus"
        title="account.planType=${escHtml(plan)}">${escHtml(plan.toUpperCase() || 'PLUS')}</span>`;
    }
    const label = (plan || 'free').toUpperCase();
    return `<span class="badge upi-plan-badge upi-plan-free"
      title="account.planType=${escHtml(plan || 'free')}">${escHtml(label)}</span>`;
  }

  // Fire 1 request check-session. Trả Promise<bool> = "đã thực sự gửi request"
  // (poller dùng để đếm đúng số lần check THẬT, không tính lần early-return).
  // force=true (poller + nút Recheck) bỏ qua cache guard j.plan_check; force=false
  // (render path mỗi giây) GIỮ guard — đây là thứ chặn flood 1 req/giây.
  function triggerPlanCheck(jobId, { force = false } = {}) {
    if (!jobId) return Promise.resolve(false);
    if (_planCheckInflight.has(jobId)) return Promise.resolve(false);
    const j = state.jobs.get(jobId);
    if (!j || j.status !== 'success') return Promise.resolve(false);
    if (!force && j.plan_check) return Promise.resolve(false);
    _planCheckInflight.add(jobId);
    return api(`/api/upi/jobs/${encodeURIComponent(jobId)}/check-session`, {
      method: 'POST',
    }).then((data) => {
      // Apply trực tiếp lên job state để render ngay; server cũng broadcast
      // job update qua SSE (plan_check field) — bên SSE handler sẽ overwrite.
      const cur = state.jobs.get(jobId);
      if (cur) {
        cur.plan_check = data;
        renderJobs();
      }
      return true;
    }).catch((err) => {
      console.warn('[upi] check-session failed:', err);
      // Đặt fake plan_check để hiện badge "PLAN ?" + tooltip lỗi.
      const cur = state.jobs.get(jobId);
      if (cur) {
        cur.plan_check = {
          ok: false, plan: null, is_plus: false, expires: null,
          checked_at: Math.floor(Date.now() / 1000),
          error: err && err.message ? err.message : 'request failed',
        };
        renderJobs();
      }
      return true;  // request đã gửi (dù lỗi) → vẫn tính 1 lần check thật
    }).finally(() => {
      _planCheckInflight.delete(jobId);
    });
  }

  // Dừng poller + xóa entry (cho phép poll lại nếu job quay về success sau này).
  function _stopPlanPoll(jobId) {
    const st = _planPollState.get(jobId);
    if (st && st.timer) clearTimeout(st.timer);
    _planPollState.delete(jobId);
  }

  // Khởi động auto-poll cho 1 job success vừa hết hạn QR. Self-guard chống
  // spawn trùng (SSE re-render gọi mỗi giây) + chống restart sau khi xong.
  function startPlanPoll(jobId) {
    if (!jobId) return;
    if (_planPollState.has(jobId)) return;  // đang poll HOẶC đã xong → không spawn lại
    const j = state.jobs.get(jobId);
    if (!j || j.status !== 'success') return;
    if (j.can_check_plan === false) return;  // mất cookies (server restart) → poll vô ích
    if (j.plan_check && j.plan_check.is_plus) return;  // đã Plus rồi
    _planPollState.set(jobId, { count: 0, timer: null });
    _planPollTick(jobId);  // check ngay lần đầu (QR vừa expired)
  }

  function _planPollTick(jobId) {
    const st = _planPollState.get(jobId);
    if (!st) return;
    const j = state.jobs.get(jobId);
    // Guard đầu tick TRƯỚC khi đọc property — job có thể bị remove/rời success.
    if (!j || j.status !== 'success') { _stopPlanPoll(jobId); return; }
    // Đã Plus / hết lượt → ngừng nhưng GIỮ entry (để không restart mỗi giây).
    if (j.plan_check && j.plan_check.is_plus) { st.timer = null; return; }
    if (st.count >= PLAN_POLL_MAX) { st.timer = null; return; }

    triggerPlanCheck(jobId, { force: true }).then((fired) => {
      if (!_planPollState.has(jobId)) return;  // bị cleanup giữa chừng
      if (fired) st.count += 1;  // chỉ đếm lần check THẬT
      const after = state.jobs.get(jobId);
      if (!after || after.status !== 'success') { _stopPlanPoll(jobId); return; }
      if ((after.plan_check && after.plan_check.is_plus) || st.count >= PLAN_POLL_MAX) {
        st.timer = null;  // xong: giữ entry, không restart
        return;
      }
      // Lên lịch tick kế ~20s TỪ completion (không phải timer song song).
      st.timer = setTimeout(() => _planPollTick(jobId), PLAN_POLL_INTERVAL_MS);
    });
  }

  // Định dạng thời điểm hết hạn theo timezone (VN / IN).
  function fmtExpiryAt(expiresAt, tz) {
    if (!expiresAt) return '-';
    try {
      return new Date(expiresAt * 1000).toLocaleString('vi-VN', {
        timeZone: tz, hour12: false,
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    } catch (_) {
      return new Date(expiresAt * 1000).toISOString();
    }
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
    return fetch(path, { ...opts, headers }).then((r) => {
      if (!r.ok) return r.text().then((t) => { throw new Error(`HTTP ${r.status}: ${t}`); });
      return r.json();
    });
  }

  // ── QR blob cache: tải PNG về local 1 lần khi job success, sau đó
  // dùng Blob URL cho <img>/download/open. Không gọi /api/upi/jobs/{id}/qr
  // mỗi lần mở modal. Revoke khi job remove/retry để tránh memory leak.
  // ────────────────────────────────────────────────────────────────────
  const qrBlobCache = new Map();   // jobId → { url, finished_at, contentType }
  const qrFetchPromise = new Map(); // jobId → in-flight Promise (dedup)

  function _qrApiUrl(jobId) {
    const token = window.GptUi.getAuthToken();
    return `/api/upi/jobs/${encodeURIComponent(jobId)}/qr` + (token ? `?token=${encodeURIComponent(token)}` : '');
  }

  function fetchQrBlob(jobId, finishedAt) {
    // Cache hit và finished_at chưa đổi → trả entry cũ.
    const cached = qrBlobCache.get(jobId);
    if (cached && cached.finished_at === finishedAt) {
      return Promise.resolve(cached);
    }
    // Có in-flight cho job này → reuse.
    if (qrFetchPromise.has(jobId)) {
      return qrFetchPromise.get(jobId);
    }
    // Có cache cũ với finished_at khác → revoke trước khi fetch lại.
    if (cached) {
      try { URL.revokeObjectURL(cached.url); } catch (_) {}
      qrBlobCache.delete(jobId);
    }
    const token = window.GptUi.getAuthToken();
    const promise = fetch(_qrApiUrl(jobId), {
      headers: token ? { 'X-API-Token': token } : {},
    }).then((r) => {
      if (!r.ok) throw new Error(`QR fetch HTTP ${r.status}`);
      const ct = r.headers.get('content-type') || 'image/png';
      return r.blob().then((blob) => ({ blob, ct }));
    }).then(({ blob, ct }) => {
      const url = URL.createObjectURL(blob);
      // Lưu cả blob lẫn url: url cho <img>/window.open, blob cho clipboard
      // copy (Clipboard API yêu cầu Blob trực tiếp, không nhận blob URL).
      const entry = { url, blob, finished_at: finishedAt, contentType: ct };
      qrBlobCache.set(jobId, entry);
      return entry;
    }).finally(() => {
      qrFetchPromise.delete(jobId);
    });
    qrFetchPromise.set(jobId, promise);
    return promise;
  }

  function revokeQrBlob(jobId) {
    const entry = qrBlobCache.get(jobId);
    if (entry) {
      try { URL.revokeObjectURL(entry.url); } catch (_) {}
      qrBlobCache.delete(jobId);
    }
    qrFetchPromise.delete(jobId);
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
    window.GptUi.persistTextarea(LS_INPUT_UPI, dom.comboInput.value);
  });

  // ── Render job list (compact 1-row giống Get Session) ─────────────
  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">Paste accounts and click Get UPI QR.</div>';
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
      // QR icon — chỉ active khi success+has_qr
      if (j.status === 'success' && j.has_qr) {
        actionBtns += `<button class="icon-btn icon-accent" data-action="view-qr" data-id="${escHtml(id)}" title="Xem QR">${window.GptUi.icon('qr')}</button>`;
      }
      // Action chính theo status
      if (j.status === 'running') {
        actionBtns += `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${window.GptUi.icon('stop')}</button>`;
      } else {
        actionBtns += `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${window.GptUi.icon('retry')}</button>`;
      }
      if (j.status === 'success' && j.has_qr) {
        actionBtns += `<button class="icon-btn" data-action="copy-qr-img" data-id="${escHtml(id)}" title="Copy ảnh QR vào clipboard">${window.GptUi.icon('copy')}</button>`;
      }
      // Recheck plan: force check-session ngay (bỏ qua cache), kể cả khi đã có
      // plan_check — cho user ép kiểm tra lại sau khi UPI pump lên Plus.
      if (j.status === 'success') {
        actionBtns += `<button class="icon-btn upi-recheck-btn" data-action="recheck-plan" data-id="${escHtml(id)}" title="Recheck plan">${window.GptUi.icon('verify')}</button>`;
      }
      actionBtns += `<button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>`;

      const amountBadge = j.amount
        ? `<span class="badge badge-muted upi-amount" title="amount inr">${escHtml(fmtAmount(j.amount))}</span>`
        : '';
      const countdownBadge = (j.status === 'success' && j.qr_expires_at)
        ? `<span class="badge upi-countdown-badge" data-exp="${escHtml(String(j.qr_expires_at))}" title="QR hết hạn sau"></span>`
        : '';
      const planBadge = renderPlanBadge(j);
      // Badge re-login cycle: chỉ hiện khi đã chạy >1 cycle (all-blocked → re-login).
      const cycleBadge = (j.cycle_count && j.cycle_count > 1)
        ? `<span class="badge upi-cycle-badge" title="Đã re-login ${escHtml(String(j.cycle_count))} lần (approve bị IP/edge-block)">↻ ${escHtml(String(j.cycle_count))}</span>`
        : '';
      const errBadge = (j.status === 'error' && j.error)
        ? `<span class="upi-err-inline" title="${escHtml(j.error)}">${escHtml(j.error.slice(0, 60))}</span>`
        : '';

      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(j.status)}">${escHtml(j.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(j.email)}">
              <span class="job-email-text">${escHtml(j.email)}</span>
              ${amountBadge}
              ${countdownBadge}
              ${planBadge}
              ${cycleBadge}
              ${errBadge}
            </div>
          </div>
          <div class="job-duration">${escHtml(fmtDuration(j.duration))}</div>
          <div class="job-actions">${actionBtns}</div>
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
    updateCountdowns();
  }

  // ── Render outputs ────────────────────────────────────────────────
  // Output pane (xanh): liệt kê acc đã verify Plus dạng `email|password|secret`.
  // Điều kiện: j.plan_check.is_plus === true (Q1=B). Bao gồm cả job từ
  // _plus_cache (status='success' từ add_jobs hit cache) và job tự upgrade
  // qua poller check_plan.
  // Error pane (đỏ): job status='error' && KHÔNG phải Plus (tránh double-count
  // case timeout-but-plus → đã hiển thị ở Output rồi).
  //
  // 2 nguồn lookup secret theo thứ tự ưu tiên:
  //   1. _pastedSecretsByEmail — parse trực tiếp từ textarea lúc user click
  //      Run; có NGAY, không phụ thuộc network. Persist qua localStorage để
  //      sống qua reload.
  //   2. secretsCache — fetch async từ /api/upi/jobs/secrets (fallback khi
  //      input bị clear hoặc reload mất localStorage).
  // Ưu tiên (1) vì đảm bảo render đúng ngay lần đầu, KHÔNG phụ thuộc race
  // condition fetch debounce 150ms.
  const secretsCache = new Map();
  // Map<emailLower, {password, secret}> — pre-populated từ paste input.
  const _pastedSecretsByEmail = new Map();
  let _secretsRefreshScheduled = false;

  // localStorage key — persist pasted secrets qua reload (lifecycle ngắn,
  // đến khi user clear input hoặc clear localStorage).
  const LS_PASTED_SECRETS = 'gpt_reg.upi.pasted_secrets';

  function _persistPastedSecrets() {
    try {
      const obj = {};
      for (const [k, v] of _pastedSecretsByEmail.entries()) obj[k] = v;
      localStorage.setItem(LS_PASTED_SECRETS, JSON.stringify(obj));
    } catch (_) { /* quota — ignore */ }
  }

  function _loadPastedSecrets() {
    try {
      const raw = localStorage.getItem(LS_PASTED_SECRETS);
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
    // Debounce ~150ms — gom nhiều SSE event (job/status/log) vào 1 fetch.
    setTimeout(() => {
      _secretsRefreshScheduled = false;
      refreshSecrets();
    }, 150);
  }

  function refreshSecrets() {
    return api('/api/upi/jobs/secrets').then((data) => {
      const map = data.secrets || {};
      // Replace entire cache (server là source of truth — job removed sẽ
      // biến mất khỏi map). Không merge vì stale entry sẽ leak qua copy.
      secretsCache.clear();
      for (const id of Object.keys(map)) {
        secretsCache.set(id, map[id] || {});
      }
      renderOutputs();
    }).catch((err) => {
      console.warn('[upi] refreshSecrets failed:', err && err.message);
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

  function renderOutputs() {
    const successLines = [];
    const errorLines = [];
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j) continue;
      const isPlus = !!(j.plan_check && j.plan_check.is_plus);
      if (isPlus) {
        const { password, secret } = _resolveSecretsFor(j);
        // Format `email|password|secret_2fa` đồng nhất với Reg + format input
        // UPI. Nếu thiếu secret (acc không 2FA) → bỏ field thay vì để pipe trống
        // → giữ output paste-back được.
        if (password) {
          successLines.push(secret
            ? `${j.email}|${password}|${secret}`
            : `${j.email}|${password}`);
        } else {
          // Cả pasted map lẫn secretsCache đều không có → render placeholder
          // để user thấy đang loading thay vì line trống `email|`.
          successLines.push(`${j.email}  (đang tải secrets...)`);
        }
      } else if (j.status === 'error' && j.error) {
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
    api(`/api/upi/jobs/${jobId}/log`).then((data) => {
      const lines = data.log || [];
      // Mỗi span tự kết thúc bằng '\n' (giống applyLog) để khi SSE append
      // span mới sẽ không bị dính vào span cuối.
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal|threshold)/i.test(l) ? 'log-line-error' : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}\n</span>`;
      }).join('');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── QR Modal ──────────────────────────────────────────────────────
  let _modalActiveJobId = null;
  let _modalExpiresAt = null;

  // Set các dòng hết hạn (VN/IN) trong modal + lưu mốc để countdown tick.
  function _setModalExpiry(expiresAt) {
    _modalExpiresAt = expiresAt || null;
    dom.modalExpVn.textContent = fmtExpiryAt(expiresAt, 'Asia/Ho_Chi_Minh');
    dom.modalExpIn.textContent = fmtExpiryAt(expiresAt, 'Asia/Kolkata');
    _tickModalCountdown();
  }

  // Set link thanh toán hosted (payments.stripe.com/upi/instructions/...).
  // Hiện anchor + nút copy khi có; ẩn khi không.
  function _setModalPayLink(url) {
    const has = !!url;
    if (dom.modalPayLink) {
      dom.modalPayLink.style.display = has ? '' : 'none';
      if (has) dom.modalPayLink.href = url;
    }
    if (dom.modalPayLinkEmpty) dom.modalPayLinkEmpty.style.display = has ? 'none' : '';
    if (dom.modalCopyLink) {
      dom.modalCopyLink.style.display = has ? '' : 'none';
      dom.modalCopyLink.dataset.link = url || '';
    }
  }

  function _tickModalCountdown() {
    if (dom.modal.style.display === 'none') return;
    const cd = fmtCountdown(_modalExpiresAt);
    dom.modalCountdown.textContent = _modalExpiresAt ? (cd.text || '-') : '-';
    dom.modalCountdown.classList.toggle('upi-countdown-expired', cd.expired);
  }

  // Cập nhật mọi badge countdown trên job list (data-exp) + modal.
  function updateCountdowns() {
    const badges = dom.jobList.querySelectorAll('.upi-countdown-badge[data-exp]');
    badges.forEach((el) => {
      const exp = parseInt(el.dataset.exp, 10);
      const cd = fmtCountdown(exp);
      el.textContent = cd.text;
      el.classList.toggle('upi-countdown-expired', cd.expired);
      // Vừa cross 0 → khởi động auto-poll (self-guard chống spawn trùng).
      if (cd.expired) {
        const row = el.closest('[data-id]');
        if (row && row.dataset.id) {
          const jobId = row.dataset.id;
          startPlanPoll(jobId);
          // Auto open QR modal + copy QR + recheck — chỉ 1 lần per job.
          if (!_autoTriggeredOnExpiry.has(jobId)) {
            _autoTriggeredOnExpiry.add(jobId);
            console.log('[upi] auto-trigger on expiry:', jobId);
            const j = state.jobs.get(jobId);
            if (j && j.has_qr) {
              // Delay nhẹ để DOM ổn định sau renderJobs innerHTML replace
              setTimeout(() => {
                copyQrToClipboard(jobId);
                triggerPlanCheck(jobId, { force: true });
              }, 300);
            } else {
              triggerPlanCheck(jobId, { force: true });
            }
          }
        }
      }
    });
    _tickModalCountdown();
  }

  function openQrModal(jobId) {
    const j = state.jobs.get(jobId);
    if (!j || !j.has_qr) return;
    _modalActiveJobId = jobId;    dom.modalEmail.textContent = j.email;
    dom.modalAmount.textContent = j.amount ? fmtAmount(j.amount) : '-';
    dom.modalSource.textContent = j.qr_source || '-';
    dom.modalCs.textContent = j.checkout_session || '-';
    _setModalPayLink(j.payment_link);
    _setModalExpiry(j.qr_expires_at);
    dom.modal.style.display = 'flex';
    if (dom.modalOk) dom.modalOk.focus();

    // Lấy ảnh từ Blob cache; nếu chưa có → fetch về (dùng spinner placeholder).
    const finishedAt = j.finished_at || 0;
    const cached = qrBlobCache.get(jobId);
    if (cached && cached.finished_at === finishedAt) {
      dom.modalImg.src = cached.url;
      return;
    }
    dom.modalImg.removeAttribute('src');
    fetchQrBlob(jobId, finishedAt).then((entry) => {
      // Verify modal vẫn đang mở cùng job → set src (tránh race khi user đóng / mở job khác).
      if (_modalActiveJobId === jobId) {
        dom.modalImg.src = entry.url;
      }
    }).catch((err) => {
      if (_modalActiveJobId === jobId) {
        dom.modalImg.removeAttribute('src');
      }
      Dialog.alert({ message: 'Tải QR thất bại: ' + err.message }).catch(() => {});
    });
  }

  function closeQrModal() {
    dom.modal.style.display = 'none';
    dom.modalImg.removeAttribute('src');
    _modalActiveJobId = null;
    _modalExpiresAt = null;
  }

  dom.modalClose.addEventListener('click', closeQrModal);
  dom.modalOk.addEventListener('click', closeQrModal);
  dom.modal.addEventListener('click', (e) => {
    if (e.target === dom.modal) closeQrModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dom.modal.style.display !== 'none') {
      closeQrModal();
    }
  });

  // Copy link thanh toán hosted (payments.stripe.com/upi/instructions/...).
  if (dom.modalCopyLink) {
    dom.modalCopyLink.addEventListener('click', () => {
      const url = dom.modalCopyLink.dataset.link || '';
      if (!url) {
        window.GptUi.toast('Chưa có link thanh toán', { type: 'warn' });
        return;
      }
      window.GptUi.copyWithToast(url, 'Đã copy link thanh toán');
    });
  }

  // Copy QR image vào clipboard (PNG). Tái dùng cho nút trong modal + nút copy
  // ở job item. Yêu cầu Clipboard API (HTTPS/localhost). SVG QR → convert PNG
  // canvas trước (Clipboard API không nhận image/svg+xml mọi trình duyệt).
  async function copyQrToClipboard(jobId) {
    if (!jobId) return;
    const j = state.jobs.get(jobId);
    const finishedAt = (j && j.finished_at) || 0;
    if (!navigator.clipboard || !window.ClipboardItem) {
      window.GptUi.toast(
        'Trình duyệt không hỗ trợ copy ảnh (cần HTTPS/localhost + Chrome/Edge/Safari)',
        { type: 'error' },
      );
      return;
    }
    try {
      const entry = await fetchQrBlob(jobId, finishedAt);
      let pngBlob = entry.blob;
      if (!pngBlob || !pngBlob.type || !pngBlob.type.includes('png')) {
        pngBlob = await _blobToPng(entry.url);
      }
      await navigator.clipboard.write([
        new ClipboardItem({ 'image/png': pngBlob }),
      ]);
      window.GptUi.toast('Đã copy QR vào clipboard', { type: 'success' });
    } catch (err) {
      window.GptUi.toast(
        'Copy QR thất bại: ' + (err && err.message ? err.message : err),
        { type: 'error' },
      );
    }
  }

  dom.modalCopyImg.addEventListener('click', () => copyQrToClipboard(_modalActiveJobId));

  // Convert image at given URL → PNG Blob qua canvas. Dùng cho case QR là SVG.
  // CORS-safe: blob URL same-origin, canvas exportable.
  function _blobToPng(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = img.naturalWidth || img.width || 512;
        canvas.height = img.naturalHeight || img.height || 512;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
          reject(new Error('canvas 2d context unavailable'));
          return;
        }
        ctx.drawImage(img, 0, 0);
        canvas.toBlob((blob) => {
          if (blob) resolve(blob);
          else reject(new Error('toBlob returned null'));
        }, 'image/png');
      };
      img.onerror = () => reject(new Error('image decode failed'));
      img.src = url;
    });
  }

  dom.modalOpen.addEventListener('click', () => {
    if (!_modalActiveJobId) return;
    const j = state.jobs.get(_modalActiveJobId);
    const finishedAt = (j && j.finished_at) || 0;
    fetchQrBlob(_modalActiveJobId, finishedAt).then((entry) => {
      window.open(entry.url, '_blank', 'noopener');
    }).catch((err) => Dialog.alert({ message: 'Open fail: ' + err.message }).catch(() => {}));
  });

  // ── Highlight dòng input tương ứng với job đang chọn ──────────────
  // Format combo UPI: email|password|secret — match phần email (lower).
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
      offset += line.length + 1; // +1 cho '\n'
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

  // ── Job list actions ──────────────────────────────────────────────
  dom.jobList.addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      e.stopPropagation();
      if (action === 'retry') {
        const j = state.jobs.get(id);
        // Cached Plus job (Q-A flow): yêu cầu xác nhận → DELETE cache → POST retry.
        // from_cache=true chỉ set ở backend khi add_jobs hit _plus_cache;
        // job tự lên Plus qua check_plan KHÔNG có cờ này nên không bị block.
        if (j && j.plan_check && j.plan_check.from_cache) {
          (async () => {
            const ok = await Dialog.confirm({
              message: `Acc ${j.email} đã verify Plus (cached). Vẫn chạy lại flow UPI?\n\nXoá cache và chạy probe mới.`,
            });
            if (!ok) return;
            try {
              await api(`/api/upi/plus/${encodeURIComponent(j.email)}`, {
                method: 'DELETE',
              });
            } catch (err) {
              console.warn('[upi] clear plus cache failed:', err && err.message);
              // Tiếp tục retry — backend retry sẽ enqueue + chạy login lại,
              // is_plus chỉ ảnh hưởng add_jobs, không block retry. Cache fail
              // chỉ là vấn đề nếu user re-paste sau.
            }
            api(`/api/upi/jobs/${id}/retry`, { method: 'POST' })
              .catch(async (err) => { await Dialog.alert({ message: err.message }); });
          })();
        } else {
          api(`/api/upi/jobs/${id}/retry`, { method: 'POST' })
            .catch(async (err) => { await Dialog.alert({ message: err.message }); });
        }
      } else if (action === 'stop' || action === 'remove') {
        api(`/api/upi/jobs/${id}`, { method: 'DELETE' })
          .catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'view-qr') {
        openQrModal(id);
      } else if (action === 'copy-qr-img') {
        copyQrToClipboard(id);
      } else if (action === 'recheck-plan') {
        window.GptUi.toast('Đang kiểm tra lại plan…', { type: 'info' });
        triggerPlanCheck(id, { force: true });
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

  // ── Run button ────────────────────────────────────────────────────
  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) { await Dialog.alert({ message: 'Paste accounts first.' }); return; }
    // Pre-populate _pastedSecretsByEmail TRƯỚC khi POST add_jobs — đảm bảo
    // khi SSE job event đầu tiên về, renderOutputs có ngay password/secret
    // không phải đợi /api/upi/jobs/secrets fetch debounce 150ms.
    _capturePastedSecrets(combos);
    dom.btnRun.disabled = true;
    try {
      const _modeMap = {
        single: 1, multi: 2, multi3: 3, multi5: 5, multi10: 10,
        multi20: 20, multi30: 30, multi50: 50,
        multi100: 100, multi200: 200,
      };
      const target = _modeMap[document.getElementById('mode').value] || 1;
      const approveRetries = parseInt(dom.approveRetries.value, 10) || 500;
      const approveRetryDelay = parseInt(dom.approveRetryDelay.value, 10) || 5;
      const restartThreshold = parseInt(dom.restartThreshold.value, 10);
      const maxRestarts = parseInt(dom.maxRestarts.value, 10);
      const jobTimeout = parseInt(dom.jobTimeout.value, 10) || 1800;
      const proxyFromStep = parseInt(dom.proxyFromStep.value, 10) || 3;
      const maxOuterCycles = parseInt(dom.maxOuterCycles.value, 10) || 1;
      const reloginBlockStreak = parseInt(dom.reloginBlockStreak.value, 10);
      const loginProxyUrl = (dom.loginProxyUrl.value || '').trim();
      await api('/api/upi/config', {
        method: 'POST',
        body: JSON.stringify({
          max_concurrent: target,
          job_timeout: jobTimeout,
          approve_retries: approveRetries,
          approve_retry_delay: approveRetryDelay,
          restart_threshold: isNaN(restartThreshold) ? 1 : restartThreshold,
          max_restarts: isNaN(maxRestarts) ? 500 : maxRestarts,
          proxy_from_step: proxyFromStep,
          max_outer_cycles: maxOuterCycles,
          relogin_block_streak: isNaN(reloginBlockStreak) ? 12 : reloginBlockStreak,
          login_proxy_url: loginProxyUrl,
        }),
      });
      await api('/api/upi/jobs', {
        method: 'POST',
        body: JSON.stringify({ combos }),
      });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
    }
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    window.GptUi.clearPersistedTextarea(LS_INPUT_UPI);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    try { await api('/api/upi/jobs/stop-all', { method: 'POST' }); }
    catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.btnClearDone.addEventListener('click', async () => {
    try { await api('/api/upi/jobs/clear-finished', { method: 'POST' }); }
    catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.btnClearAll.addEventListener('click', async () => {
    if (!(await Dialog.confirm({
      message: 'Xoá TẤT CẢ jobs UPI (mọi trạng thái)? Hành động không thể hoàn tác.',
      danger: true,
      confirmLabel: 'Xoá',
    }))) return;
    try {
      const res = await api('/api/upi/jobs/clear-all', { method: 'POST' });
      console.log('[upi] clear-all:', res.removed);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  dom.btnClearCookies.addEventListener('click', async () => {
    if (!(await Dialog.confirm({
      message: 'Xoá toàn bộ cookie cache UPI? Lần chạy tiếp theo sẽ login lại từ đầu.',
      danger: true,
      confirmLabel: 'Xoá',
    }))) return;
    try {
      const res = await api('/api/upi/cookies', { method: 'DELETE' });
      const n = (res && typeof res.cleared === 'number') ? res.cleared : 0;
      window.GptUi?.toast?.(`Đã xoá ${n} cookie cache`, { type: 'success' });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  dom.btnRetryFailed.addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Retry tất cả jobs error & cancelled?' }))) return;
    try {
      const res = await api('/api/upi/jobs/retry-failed', { method: 'POST' });
      console.log('[upi] retry-failed:', res.retried);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  dom.btnRetryExpiredFree.addEventListener('click', async () => {
    // Đếm preview ở client để Dialog hỏi có ngữ cảnh số job sẽ retry. Server
    // sẽ filter lại bằng đúng predicate (xem retry_expired_free) — count
    // client chỉ là hint, có thể lệch nhẹ nếu state vừa đổi giữa render
    // và click.
    const now = Date.now() / 1000;
    let count = 0;
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j || j.status !== 'success') continue;
      if (!j.qr_expires_at || j.qr_expires_at >= now) continue;
      const pc = j.plan_check;
      if (!pc || pc.ok !== true || pc.is_plus) continue;
      count++;
    }
    if (count === 0) {
      await Dialog.alert({
        message: 'Không có job nào hết hạn QR mà vẫn Free.\n\nĐiều kiện: success + qr_expired + plan_check ok=true + is_plus=false.',
      }).catch(() => {});
      return;
    }
    if (!(await Dialog.confirm({ message: `Retry ${count} job hết hạn + Free?` }))) return;
    try {
      const res = await api('/api/upi/jobs/retry-expired-free', { method: 'POST' });
      console.log('[upi] retry-expired-free:', res.retried);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  dom.approveRetries.addEventListener('change', async () => {
    const val = parseInt(dom.approveRetries.value, 10);
    if (isNaN(val) || val < 1) return;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ approve_retries: val }),
      });
      state.approveRetries = val;
    } catch (err) { console.error(err); }
  });

  dom.approveRetryDelay.addEventListener('change', async () => {
    // Clamp client-side trước khi POST để UX rõ (input min=2).
    let val = parseInt(dom.approveRetryDelay.value, 10);
    if (isNaN(val) || val < 2) {
      val = 2;
      dom.approveRetryDelay.value = '2';
    } else if (val > 60) {
      val = 60;
      dom.approveRetryDelay.value = '60';
    }
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ approve_retry_delay: val }),
      });
      state.approveRetryDelay = val;
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được retry delay: ' + err.message });
    }
  });

  dom.restartThreshold.addEventListener('change', async () => {
    // Số `result=exception` LIÊN TIẾP để restart checkout. 0 = tắt restart.
    let val = parseInt(dom.restartThreshold.value, 10);
    if (isNaN(val) || val < 0) {
      val = 0;
      dom.restartThreshold.value = '0';
    } else if (val > 1000) {
      val = 1000;
      dom.restartThreshold.value = '1000';
    }
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ restart_threshold: val }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được restart_threshold: ' + err.message });
    }
  });

  dom.maxRestarts.addEventListener('change', async () => {
    // Số lần restart tối đa / job. 0 = tắt restart.
    let val = parseInt(dom.maxRestarts.value, 10);
    if (isNaN(val) || val < 0) {
      val = 0;
      dom.maxRestarts.value = '0';
    } else if (val > 2000) {
      val = 2000;
      dom.maxRestarts.value = '2000';
    }
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ max_restarts: val }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được max_restarts: ' + err.message });
    }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 60) return;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) { console.error(err); }
  });

  dom.proxyFromStep.addEventListener('change', async () => {
    const val = parseInt(dom.proxyFromStep.value, 10);
    if (isNaN(val) || val < 1 || val > 6) return;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ proxy_from_step: val }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được proxy_from_step: ' + err.message });
    }
  });

  dom.maxOuterCycles.addEventListener('change', async () => {
    let val = parseInt(dom.maxOuterCycles.value, 10);
    if (isNaN(val) || val < 1) { val = 1; dom.maxOuterCycles.value = '1'; }
    else if (val > 5) { val = 5; dom.maxOuterCycles.value = '5'; }
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ max_outer_cycles: val }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được max_outer_cycles: ' + err.message });
    }
  });

  dom.reloginBlockStreak.addEventListener('change', async () => {
    let val = parseInt(dom.reloginBlockStreak.value, 10);
    if (isNaN(val) || val < 0) { val = 12; dom.reloginBlockStreak.value = '12'; }
    else if (val > 1000) { val = 1000; dom.reloginBlockStreak.value = '1000'; }
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ relogin_block_streak: val }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được relogin_block_streak: ' + err.message });
    }
  });

  // Login proxy URL: lưu trên blur/change (Enter). Empty string = clear,
  // backend normalize "" và áp luồng cũ. Format invalid → backend trả 400
  // với message từ UpiJobManager.set_login_proxy_url validate.
  dom.loginProxyUrl.addEventListener('change', async () => {
    const raw = (dom.loginProxyUrl.value || '').trim();
    dom.loginProxyUrl.value = raw;  // normalize hiển thị
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ login_proxy_url: raw }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được login_proxy_url: ' + err.message });
    }
  });

  // session.login_flow đã chuyển sang tab Settings (settings_panel.js) — setting
  // toàn cục, không quản ở đây nữa.

  dom.notifyToggle.addEventListener('change', async () => {
    const enabled = dom.notifyToggle.checked;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ notify_enabled: enabled }),
      });
    } catch (err) {
      dom.notifyToggle.checked = !enabled; // revert nếu fail
      await Dialog.alert({ message: 'Không lưu được toggle: ' + err.message });
    }
  });

  dom.btnCopyError.addEventListener('click', () => {
    window.GptUi.copyText(dom.errorPane.textContent);
  });

  dom.btnCopySuccess.addEventListener('click', () => {
    window.GptUi.copyText(dom.successPane.textContent);
  });

  // ── SSE ───────────────────────────────────────────────────────────
  function _maybePrefetchQr(j) {
    // Job vừa success + có QR → tải blob về cache local ngay (không đợi user mở modal).
    // Nếu finished_at thay đổi (retry) → fetchQrBlob tự revoke entry cũ.
    if (j && j.has_qr) {
      fetchQrBlob(j.id, j.finished_at || 0).catch(() => {
        // Best-effort — log fail không làm vỡ flow.
      });
    }
  }

  function applySnapshot(snap) {
    state.maxConcurrent = snap.max_concurrent || state.maxConcurrent;
    state.approveRetries = snap.approve_retries || state.approveRetries;
    if (snap.approve_retries) dom.approveRetries.value = snap.approve_retries;
    if (snap.approve_retry_delay) {
      dom.approveRetryDelay.value = snap.approve_retry_delay;
      state.approveRetryDelay = snap.approve_retry_delay;
    }
    if (typeof snap.restart_threshold === 'number') {
      dom.restartThreshold.value = snap.restart_threshold;
    }
    if (typeof snap.max_restarts === 'number') {
      dom.maxRestarts.value = snap.max_restarts;
    }
    if (snap.job_timeout) dom.jobTimeout.value = snap.job_timeout;
    if (snap.proxy_from_step) dom.proxyFromStep.value = String(snap.proxy_from_step);
    if (typeof snap.max_outer_cycles === 'number') {
      dom.maxOuterCycles.value = snap.max_outer_cycles;
    }
    if (typeof snap.login_proxy_url === 'string') {
      dom.loginProxyUrl.value = snap.login_proxy_url;
    }

    // Revoke blob cho job không còn trong snapshot (cleanup khi server clear).
    const incomingIds = new Set(snap.jobs.map((j) => j.id));
    for (const cachedId of Array.from(qrBlobCache.keys())) {
      if (!incomingIds.has(cachedId)) revokeQrBlob(cachedId);
    }

    state.order = snap.jobs.map((j) => j.id);
    state.jobs.clear();
    for (const j of snap.jobs) {
      state.jobs.set(j.id, j);
      _maybePrefetchQr(j);
      // Job đã expired trước khi page load → không auto-trigger open/copy/recheck
      if (j.status === 'success' && j.qr_expires_at) {
        const cd = fmtCountdown(j.qr_expires_at);
        if (cd.expired) _autoTriggeredOnExpiry.add(j.id);
      }
    }
    renderJobs();
    renderOutputs();
    // Snapshot có thể chứa job mới (cached Plus từ add_jobs); cần fetch
    // password/secret để render Output pane đúng format.
    scheduleSecretsRefresh();
  }

  function applyJobUpdate(j) {
    const prev = state.jobs.get(j.id);
    if (!prev) state.order.push(j.id);
    state.jobs.set(j.id, j);

    // Job rời success (retry → running, error, …) → dừng poller để khỏi leak
    // timer + tránh tick đọc job đã đổi trạng thái.
    if (j.status !== 'success') _stopPlanPoll(j.id);

    // Job retry (mất has_qr) hoặc QR mới (finished_at đổi) → revoke entry cũ.
    if (prev && prev.has_qr && (!j.has_qr || prev.finished_at !== j.finished_at)) {
      revokeQrBlob(j.id);
    }
    _maybePrefetchQr(j);

    renderJobs();
    renderOutputs();
    if (state.activeJobId === j.id) renderLog(j.id);

    // Modal đang mở cho job này + QR mới về → cập nhật src + meta.
    if (_modalActiveJobId === j.id) {
      dom.modalAmount.textContent = j.amount ? fmtAmount(j.amount) : '-';
      dom.modalSource.textContent = j.qr_source || '-';
      dom.modalCs.textContent = j.checkout_session || '-';
      _setModalPayLink(j.payment_link);
      _setModalExpiry(j.qr_expires_at);
      if (j.has_qr) {
        fetchQrBlob(j.id, j.finished_at || 0).then((entry) => {
          if (_modalActiveJobId === j.id) {
            dom.modalImg.src = entry.url;
          }
        }).catch(() => {});
      }
    }

    if (j.status === 'error' && (!prev || prev.status !== 'error') && window.GptUi?.playErrorAlert) {
      window.GptUi.playErrorAlert();
    }

    // Job vừa approved (success) → nổ fanfare to
    if (j.status === 'success' && (!prev || prev.status !== 'success') && window.GptUi?.playSuccessAlert) {
      window.GptUi.playSuccessAlert();
    }

    // Job mới hoặc plan_check thay đổi (đặc biệt is_plus đổi true) → cần
    // password/secret cho Output pane. Fetch secrets khi:
    //   - Job mới xuất hiện (prev null)
    //   - is_plus chuyển true (acc vừa lên Plus qua poller hoặc post-timeout)
    const wasPlus = prev && prev.plan_check && prev.plan_check.is_plus;
    const nowPlus = j.plan_check && j.plan_check.is_plus;
    if (!prev || (nowPlus && !wasPlus)) {
      scheduleSecretsRefresh();
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    revokeQrBlob(jobId);
    _stopPlanPoll(jobId);  // dọn timer poll (H1: callback sau remove sẽ TypeError + leak)
    _autoTriggeredOnExpiry.delete(jobId);
    if (state.activeJobId === jobId) { state.activeJobId = null; renderLog(null); }
    if (_modalActiveJobId === jobId) closeQrModal();
    renderJobs();
    renderOutputs();
  }

  function applyLog(jobId, line) {
    if (state.activeJobId !== jobId) return;
    const cls = /(error|FAILED|fatal|threshold)/i.test(line) ? 'log-line-error' : 'log-line-info';
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = line + '\n';
    dom.logPane.appendChild(span);
    dom.logPane.scrollTop = dom.logPane.scrollHeight;
  }

  SseBus.on('upi', (data) => {
    if (data.type === 'snapshot') applySnapshot(data);
    else if (data.type === 'job') applyJobUpdate(data.job);
    else if (data.type === 'remove') applyRemove(data.job_id);
    else if (data.type === 'clear_finished') {
      api('/api/upi/jobs').then(applySnapshot).catch(console.error);
    }
    else if (data.type === 'clear_all') {
      // Cleanup mọi resource frontend trước khi reset state — tránh leak
      // QR blob URL + timer poll plan + modal đang mở.
      for (const jid of Array.from(state.jobs.keys())) {
        revokeQrBlob(jid);
        _stopPlanPoll(jid);
      }
      state.jobs.clear();
      state.order = [];
      state.activeJobId = null;
      if (_modalActiveJobId) closeQrModal();
      renderJobs();
      renderOutputs();
      renderLog(null);
    }
    else if (data.type === 'log') applyLog(data.job_id, data.line);
  });

  // ── Init ──────────────────────────────────────────────────────────
  // Restore pasted secrets từ localStorage TRƯỚC khi load snapshot từ
  // server — đảm bảo nếu user reload page mà server vẫn giữ jobs cached
  // Plus, output render đúng email|password|secret từ ratchet local.
  _loadPastedSecrets();

  const _saved = localStorage.getItem(LS_INPUT_UPI);
  if (_saved) dom.comboInput.value = _saved;
  updateComboCount();

  api('/api/upi/config').then((cfg) => {
    if (cfg.approve_retries) dom.approveRetries.value = cfg.approve_retries;
    if (cfg.approve_retry_delay) {
      dom.approveRetryDelay.value = cfg.approve_retry_delay;
      state.approveRetryDelay = cfg.approve_retry_delay;
    }
    if (typeof cfg.restart_threshold === 'number') {
      dom.restartThreshold.value = cfg.restart_threshold;
    }
    if (typeof cfg.max_restarts === 'number') {
      dom.maxRestarts.value = cfg.max_restarts;
    }
    if (cfg.job_timeout) dom.jobTimeout.value = cfg.job_timeout;
    if (cfg.proxy_from_step) dom.proxyFromStep.value = String(cfg.proxy_from_step);
    if (typeof cfg.max_outer_cycles === 'number') {
      dom.maxOuterCycles.value = cfg.max_outer_cycles;
    }
    if (typeof cfg.relogin_block_streak === 'number') {
      dom.reloginBlockStreak.value = cfg.relogin_block_streak;
    }
    if (typeof cfg.login_proxy_url === 'string') {
      dom.loginProxyUrl.value = cfg.login_proxy_url;
    }
    state.approveRetries = cfg.approve_retries;
    dom.notifyToggle.checked = !!cfg.notify_enabled;
  }).catch(() => {});

  // session.login_flow load đã chuyển sang settings_panel.js (tab Settings).

  // Duration timer cho running jobs + countdown QR
  setInterval(() => {
    let hasRunning = false;
    for (const [, j] of state.jobs) {
      if (j.status === 'running' && j.started_at) {
        hasRunning = true;
        j.duration = (Date.now() / 1000) - j.started_at;
      }
    }
    if (hasRunning) renderJobs();
    else updateCountdowns();
  }, 1000);
})();
