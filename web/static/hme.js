/**
 * iCloud HME tab — task 6.1 (icloud-runner-loop, R13.1, R13.6).
 *
 * 3 panel hiển thị đồng thời:
 *   - Profiles: list + delete (R10.2, R10.4, R10.12, R10.13).
 *   - Run Log: SSE stream từ HmeRunner qua SseBus channel 'hme_log'
 *     (task 6.1 — thay panel Jobs cũ).
 *   - Emails: list + filter + bulk lifecycle (R10.5, R10.15).
 *
 * NOTE: Task 6.2 sẽ thêm nút Start/Stop + status badge; task 6.3 cải tiến
 * sidebar Profile pool status; task 6.4 thêm countdown next_cycle_at.
 * Task 6.1 chỉ scope log viewer + xoá toàn bộ job-manager UI cũ.
 */

(function () {
  "use strict";

  // ── Auth helper (reuse từ app.js global GptUi) ─────────────────────────
  // Backend (web/auth.py) dùng header `X-API-Token` (hoặc query `?token=`),
  // KHÔNG phải `Authorization: Bearer`. Phải khớp pattern app.js/link.js/session.js
  // — sai header → backend trả 401 ngay.
  function api(path, opts) {
    opts = opts || {};
    const token =
      (window.GptUi && window.GptUi.getAuthToken && window.GptUi.getAuthToken()) ||
      "";
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      token ? { "X-API-Token": token } : {},
      opts.headers || {}
    );
    return fetch(path, Object.assign({}, opts, { headers }));
  }

  // ── Sub-nav helper (legacy — task 6.1 chuyển sang 3-panel layout) ─────
  // initHmeSubtabs() đã removed: HTML không còn ``.hme-subtab-btn`` element
  // (task removed jobs panel + đổi grid layout). Giữ ``scrollToPanel`` vì
  // có thể được gọi từ chỗ khác trong future. Nếu KHÔNG có caller nào
  // còn dùng, có thể xoá luôn helper này (current grep cho thấy 0 caller).
  function scrollToPanel(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Helpers ────────────────────────────────────────────────────────────
  function fmtDt(iso) {
    if (!iso) return "-";
    return iso.replace("T", " ").replace(/\.\d+Z?$/, "");
  }

  function statusBadge(status) {
    const cls = {
      active: "badge-active",
      limited: "badge-limited",
      quota_full: "badge-quota-full",
      session_expired: "badge-error",
      disabled: "badge-error",
      deleted: "badge-muted",
      queued: "badge-muted",
      running: "badge-active",
      paused: "badge-warn",
      completed: "badge-success",
      failed: "badge-error",
      cancelled: "badge-muted",
      created: "badge-active",
      reconciled: "badge-active",
      deactivated: "badge-warn",
      revoked: "badge-warn",
      used_for_chatgpt: "badge-success",
      recording: "badge-active",
      saving: "badge-warn",
      cancelling: "badge-muted",
      done: "badge-success",
    }[status] || "badge-muted";
    const label = status || "-";
    return `<span class="badge ${cls}">${label}</span>`;
  }

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ── Privacy mask (anti screenshot leak) ──────────────────────────────
  // Toggle global ẩn/hiện email + apple_id ở mọi panel: Profiles card,
  // Run Log stream, Emails table, runner indicator, dialog text. Mặc định
  // BẬT (ẩn) để chụp screenshot mà ko lộ; persist qua Settings store (DB).
  //
  // Nguyên tắc:
  //   - CHỈ mask phần hiển thị (textContent / innerHTML / title).
  //   - `data-*`, `value` (checkbox), `_selectedEmails` GIỮ NGUYÊN raw để
  //     API call (delete / sync / bulk) không gãy.
  //   - Log lines lưu raw vào `dataset.rawText` để re-mask khi toggle.
  // Đọc từ Settings store (unified-settings-store R12.4).
  // Settings.get trả boolean trực tiếp (default true nếu chưa set).
  let _privacyMaskEnabled = Settings.get('hme.privacy_mask') !== false;

  function _maskLocal(s) {
    // Local-part email hoặc chuỗi non-email: giữ ký tự đầu + cuối, giữa thay *.
    const str = String(s);
    if (str.length <= 1) return str || "*";
    if (str.length <= 3) return str[0] + "*" + str[str.length - 1];
    if (str.length <= 5) return str.slice(0, 2) + "**" + str.slice(-1);
    return str.slice(0, 2) + "***" + str.slice(-1);
  }

  function _maskDomain(d) {
    // Domain `name.tld` hoặc `sub.name.tld`: mask phần name, giữ TLD chuẩn
    // để vẫn đọc được loại email.
    const str = String(d);
    const dot = str.lastIndexOf(".");
    if (dot < 0) return _maskLocal(str);
    const name = str.slice(0, dot);
    const tld = str.slice(dot); // includes dot
    if (name.length <= 1) return (name || "*") + tld;
    if (name.length <= 3) return name[0] + "**" + tld;
    return name.slice(0, 1) + "***" + tld;
  }

  function maskEmailForDisplay(s) {
    if (!s) return s;
    const str = String(s);
    const at = str.indexOf("@");
    if (at < 0) return _maskLocal(str);
    const local = str.slice(0, at);
    const domain = str.slice(at + 1);
    return _maskLocal(local) + "@" + _maskDomain(domain);
  }

  function privacyMask(s) {
    if (s == null || s === "") return s;
    if (!_privacyMaskEnabled) return s;
    return maskEmailForDisplay(s);
  }

  // Regex bắt email trong free text (log message) — không greedy, chấp nhận
  // dấu chấm trong domain. Không match escaped < > vì textContent đã thoát.
  const _EMAIL_RE = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/g;

  function privacyMaskText(text) {
    if (!_privacyMaskEnabled || !text) return text;
    return String(text).replace(_EMAIL_RE, (m) => maskEmailForDisplay(m));
  }

  function _updatePrivacyToggleBtn() {
    const btn = document.getElementById("hme-btn-privacy");
    if (!btn) return;
    if (_privacyMaskEnabled) {
      btn.textContent = "Ẩn";
      btn.title = "Đang ẩn email/apple_id (click để hiện)";
      btn.classList.remove("btn-primary");
      btn.classList.add("btn-ghost");
    } else {
      btn.textContent = "Hiện";
      btn.title = "Đang hiện email/apple_id (click để ẩn)";
      btn.classList.remove("btn-ghost");
      btn.classList.add("btn-primary");
    }
  }

  function _reapplyMaskToLogPane() {
    const pane = document.getElementById("hme-log-pane");
    if (!pane) return;
    pane.querySelectorAll(".log-line").forEach((line) => {
      const raw = line.dataset.rawText;
      if (raw == null) return; // line được append trước khi có dataset
      line.textContent = privacyMaskText(raw);
    });
  }

  function applyPrivacyMaskAll() {
    // Re-render mọi panel có hiển thị email/apple_id.
    _updatePrivacyToggleBtn();
    _reapplyMaskToLogPane();
    // Profiles + Emails: re-render từ dữ liệu server (đơn giản, đảm bảo
    // mọi chỗ — title, badge, action button — đồng bộ).
    if (typeof loadProfiles === "function") loadProfiles();
    if (typeof loadEmails === "function") loadEmails();
    // Runner indicator + per-profile badge sẽ tự refresh ở poll tick kế (2s).
    if (typeof pollRunnerStatus === "function") pollRunnerStatus();
  }

  function togglePrivacyMask() {
    _privacyMaskEnabled = !_privacyMaskEnabled;
    const token =
      (window.GptUi && window.GptUi.getAuthToken && window.GptUi.getAuthToken()) || "";
    Settings.save('hme.privacy_mask', _privacyMaskEnabled, token);
    applyPrivacyMaskAll();
  }

  function renderEmpty(tbody, cols, msg) {
    tbody.innerHTML = `<tr><td colspan="${cols}" class="muted">${escapeHtml(msg)}</td></tr>`;
  }

  // ── Profiles sidebar (task 6.3 — icloud-runner-loop R13.5) ─────────
  // Operations column (~42% width) liệt kê profiles dạng card compact:
  //   - apple_id + badge status (active xanh / limited vàng / quota_full cam /
  //     session_expired đỏ).
  //   - quota bar visualize hme_count/700 (đầy = đỏ, gần đầy = cam, ok = xanh).
  //   - Action buttons (Open / Sync / Delete) co lại 1 cụm icon-only.
  //
  // Auto-refresh:
  //   - setInterval 30s khi tab HME active.
  //   - Khi nhận LogEvent có payload.apple_id → debounce refresh trong 1s
  //     để tránh storm refresh khi 1 cycle phát nhiều log liên tiếp.
  //
  // Note: KHÔNG auto-refresh quá nhanh (< 30s) vì /api/icloud/profiles đọc
  // SQLite + có thể lock; respect backend.
  const PROFILE_REFRESH_INTERVAL_MS = 30_000;
  let _profileRefreshTimer = null;
  let _profileRefreshDebounce = null;

  function profileQuotaBarClass(hmeCount) {
    // Quota soft-cap = 700 / profile (Apple). Tô màu theo % usage.
    const pct = Math.min(100, Math.round(((hmeCount || 0) / 700) * 100));
    if (pct >= 100) return "hme-quota-bar-full";
    if (pct >= 90) return "hme-quota-bar-high";
    if (pct >= 70) return "hme-quota-bar-mid";
    return "hme-quota-bar-low";
  }

  function renderProfileCard(p) {
    // Backend (web/icloud_routes.py /profiles) trả: apple_id, profile_dir,
    // status, hme_count, limited_until, quota_retry_until, last_used_at,
    // last_error. KHÔNG trả quota_remaining → tự tính = max(0, 700 - hme_count).
    const hmeCount = p.hme_count || 0;
    const quotaRemaining = Math.max(0, 700 - hmeCount);
    const pct = Math.min(100, Math.round((hmeCount / 700) * 100));
    const barCls = profileQuotaBarClass(hmeCount);
    // RAW dùng cho data-* (action call) + escape; DISPLAY có thể được mask
    // theo toggle privacy. Title hover hiển thị bản masked để khỏi lộ khi
    // user di chuột chụp screenshot.
    const appleRaw = escapeHtml(p.apple_id);
    const appleDisplay = escapeHtml(privacyMask(p.apple_id));
    const lastErrRaw = p.last_error || "";
    const lastErrDisplay = privacyMaskText(lastErrRaw);
    const lastErr = lastErrRaw
      ? `<div class="hme-profile-last-err muted" title="${escapeHtml(lastErrDisplay)}">${escapeHtml(lastErrDisplay)}</div>`
      : "";
    // Runtime badge cycle (icloud-runner-loop revised): Runner publish state
    // map qua RunStatus.profile_states; pollRunnerStatus() gọi
    // applyProfileStatesToCards() để chèn badge này vào DOM. Render placeholder
    // ở đây để tránh layout shift khi badge xuất hiện.
    return `
      <div class="hme-profile-card" data-apple-id="${appleRaw}">
        <div class="hme-profile-card-head">
          <code class="hme-profile-apple" title="${appleDisplay}">${appleDisplay}</code>
          <div class="hme-profile-card-actions">
            <span class="hme-profile-runtime-badge" data-apple-id="${appleRaw}"></span>
            ${statusBadge(p.status)}
            <button class="btn btn-ghost btn-small hme-profile-open"
                    data-apple-id="${appleRaw}"
                    title="Mở Camoufox HEADED (R15)">Open</button>
            <button class="btn btn-ghost btn-small hme-profile-sync"
                    data-apple-id="${appleRaw}"
                    title="Sync HME list từ Apple">Sync</button>
            <button class="btn btn-ghost btn-small btn-danger-ghost hme-profile-delete"
                    data-apple-id="${appleRaw}"
                    title="Xoá profile (DB + disk)">Del</button>
          </div>
        </div>
        <div class="hme-profile-quota">
          <div class="hme-quota-bar-track">
            <div class="hme-quota-bar-fill ${barCls}" style="width: ${pct}%;"></div>
          </div>
          <div class="hme-profile-quota-text muted">
            ${hmeCount} / 700 · remain ${quotaRemaining}
          </div>
        </div>
        ${lastErr}
      </div>`;
  }

  async function loadProfiles() {
    const list = document.getElementById("hme-profiles-list");
    if (!list) return;
    const filter = document.getElementById("hme-profile-status-filter").value;
    list.innerHTML = `<div class="muted hme-profile-empty">Loading...</div>`;
    try {
      const url = filter
        ? `/api/icloud/profiles?status=${encodeURIComponent(filter)}`
        : "/api/icloud/profiles";
      const resp = await api(url);
      if (!resp.ok) {
        list.innerHTML = `<div class="muted hme-profile-empty">Error: ${resp.status} ${resp.statusText}</div>`;
        return;
      }
      const profiles = await resp.json();
      if (!profiles.length) {
        list.innerHTML = `<div class="muted hme-profile-empty">No profiles. Click + Add.</div>`;
        return;
      }
      list.innerHTML = profiles.map(renderProfileCard).join("");
      bindProfileActions();
      // Card vừa re-render — runtime badge placeholder rỗng. Force poll
      // status ngay để badge running/waiting/cooldown xuất hiện thay vì
      // chờ tick poll kế (có thể cách 2s).
      if (typeof pollRunnerStatus === "function") {
        pollRunnerStatus();
      }
    } catch (exc) {
      list.innerHTML = `<div class="muted hme-profile-empty">Fetch error: ${escapeHtml(exc.message)}</div>`;
    }
  }

  function startProfileAutoRefresh() {
    if (_profileRefreshTimer) return;
    _profileRefreshTimer = setInterval(loadProfiles, PROFILE_REFRESH_INTERVAL_MS);
  }

  function stopProfileAutoRefresh() {
    if (_profileRefreshTimer) {
      clearInterval(_profileRefreshTimer);
      _profileRefreshTimer = null;
    }
    if (_profileRefreshDebounce) {
      clearTimeout(_profileRefreshDebounce);
      _profileRefreshDebounce = null;
    }
  }

  function refreshProfilesDebounced() {
    // Debounce 1s: gom nhiều LogEvent profile transition trong cùng cycle.
    if (_profileRefreshDebounce) return;
    _profileRefreshDebounce = setTimeout(() => {
      _profileRefreshDebounce = null;
      loadProfiles();
    }, 1000);
  }

  function bindProfileActions() {
    document.querySelectorAll(".hme-profile-delete").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const appleId = btn.dataset.appleId;
        const appleDisp = privacyMask(appleId);
        const ok = await Dialog.confirm({
          title: "Xoá profile?",
          message: `Profile ${appleDisp} sẽ bị xoá khỏi DB và disk. Email rows vẫn được giữ lại.`,
          confirmLabel: "Xoá",
          danger: true,
        });
        if (!ok)
          return;
        const resp = await api(
          `/api/icloud/profiles/${encodeURIComponent(appleId)}`,
          { method: "DELETE" }
        );
        const data = await resp.json();
        await Dialog.alert({
          title: "Đã xoá profile",
          message: "Server response:",
          detail: JSON.stringify(data, null, 2),
        });
        loadProfiles();
      });
    });
    document.querySelectorAll(".hme-profile-sync").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const appleId = btn.dataset.appleId;
        const appleDisp = privacyMask(appleId);
        const ans = await Dialog.choice({
          title: "Sync profile?",
          message: `Chọn cách sync ${appleDisp}. Dry run chỉ preview diff; Run sync sẽ UPDATE DB và audit.`,
          actions: [
            { label: "Dry run", value: "dry", className: "btn btn-ghost" },
            { label: "Run sync", value: "run", className: "btn btn-primary" },
          ],
        });
        if (!ans) {
          return;
        }
        const dryRun = ans === "dry";
        const url =
          `/api/icloud/sync/${encodeURIComponent(appleId)}` +
          (dryRun ? "?dry_run=true" : "");
        const resp = await api(url, { method: "POST" });
        const data = await resp.json();
        await Dialog.alert({
          title: `Sync result: ${appleDisp}`,
          message: dryRun ? "Dry run response:" : "Run sync response:",
          detail: JSON.stringify(data, null, 2),
        });
      });
    });
    document.querySelectorAll(".hme-profile-open").forEach((btn) => {
      btn.addEventListener("click", () => {
        startOpenProfile(btn.dataset.appleId);
      });
    });
    // Disable mọi nút Open khi đã có session non-terminal đang chạy (R15.4
    // — UI side mirror invariant Open_Profile_Lock_Single).
    if (_openProfileState.sessionId) {
      document.querySelectorAll(".hme-profile-open").forEach((b) => {
        b.disabled = true;
        b.title = "Đã có Open_Profile session khác đang chạy";
      });
    }
  }

  async function loadPoolStatus() {
    const summary = document.getElementById("hme-pool-status-summary");
    summary.textContent = "Loading...";
    try {
      const resp = await api("/api/icloud/pool/status");
      if (!resp.ok) {
        summary.textContent = `Error: ${resp.status}`;
        return;
      }
      const r = await resp.json();
      const byStatus = Object.entries(r.by_status)
        .filter(([, n]) => n > 0)
        .map(([k, n]) => `${k}=${n}`)
        .join(" · ") || "no profiles";
      summary.innerHTML = `
        <strong>${byStatus}</strong> ·
        quota_remaining: ${r.total_quota_remaining}/${r.quota_soft_cap_per_account * r.profiles.length}
        ${r.low_capacity ? '· <span class="badge badge-warn">low capacity</span>' : ""}
        ${r.quota_full_count > 0 ? `· <span class="badge badge-warn">quota_full: ${r.quota_full_count}</span>` : ""}
      `;
    } catch (exc) {
      summary.textContent = `Error: ${exc.message}`;
    }
  }

  function loadHmeDashboard() {
    loadProfiles();
    loadPoolStatus();
    startRunnerStatusPoll();
    startProfileAutoRefresh();
    loadEmails();
    loadRunnerConfig();
  }

  // ── Run Log panel — unified SSE via SseBus (channel: hme_log) ────────
  // Replaces legacy per-module EventSource. SseBus.connect() is called from
  // app.js on page load; this module only registers the channel handler.

  function pad2(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  function fmtLogTs(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return (
      pad2(d.getHours()) +
      ":" +
      pad2(d.getMinutes()) +
      ":" +
      pad2(d.getSeconds())
    );
  }

  function appendLogLine(event) {
    const pane = document.getElementById("hme-log-pane");
    if (!pane) return;
    const level = (event && event.level) || "info";
    const cls =
      level === "error"
        ? "log-line log-line-error"
        : level === "warn"
        ? "log-line log-line-warn"
        : "log-line log-line-info";
    const ts = fmtLogTs(event && event.ts);
    const msg = (event && event.message) || "";
    const rawText = `[${ts}][${level}] ${msg}`;
    const line = document.createElement("div");
    line.className = cls;
    // Lưu raw vào dataset để re-mask khi toggle privacy không cần reload SSE.
    line.dataset.rawText = rawText;
    line.textContent = privacyMaskText(rawText);
    pane.appendChild(line);
    // Auto-scroll xuống cuối khi nhận event mới (R13.1).
    pane.scrollTop = pane.scrollHeight;
  }

  function appendLocalNotice(message, level) {
    appendLogLine({
      ts: new Date().toISOString(),
      level: level || "info",
      message: message,
    });
  }

  // Register hme_log channel handler via SseBus (unified SSE mux).
  // SseBus.connect() is called once from app.js — no per-module connect/disconnect.
  SseBus.on('hme_log', (data) => {
    appendLogLine(data);
    // task 6.3 — khi event chứa profile transition (payload.apple_id),
    // refresh sidebar profiles để badge / quota bar phản ánh state mới
    // sớm hơn so với chờ 30s auto-refresh tick.
    if (data && data.payload && data.payload.apple_id) {
      refreshProfilesDebounced();
    }
    // Auto-reload emails khi cycle done có tạo email mới
    if (data && data.message && data.message.includes("done:") && data.payload && data.payload.result) {
      const result = data.payload.result;
      if (result.created > 0) {
        _emailPage = 0;
        loadEmails();
        loadProfiles();
      }
    }
  });

  function clearLogPane() {
    const pane = document.getElementById("hme-log-pane");
    if (pane) pane.innerHTML = "";
  }

  // ── Runner controls — Start/Stop + status badge + stats live (task 6.2) ──
  // Endpoints: POST /api/icloud/run, POST /api/icloud/run/stop, GET /api/icloud/run/status
  // Auth: dùng X-API-Token qua hàm api() (chung pattern với panel khác).
  //
  // Polling lifecycle:
  //   - startRunnerStatusPoll(): setInterval 2s → cập nhật badge + stats + nút.
  //   - stopRunnerStatusPoll(): clearInterval khi user rời tab HME.
  //
  // Backend invariant (R13.3, R13.4): UI chỉ phản ánh trạng thái server, không
  // tự assume; mọi quyết định disable Start/Stop dựa vào response /status.
  let _runnerStatusTimer = null;

  function setRunnerStatusBadge(running) {
    const el = document.getElementById("hme-runner-status-badge");
    if (!el) return;
    el.textContent = running ? "RUNNING" : "IDLE";
    el.classList.remove("badge-active", "badge-muted");
    el.classList.add(running ? "badge-active" : "badge-muted");
  }

  function setRunnerButtons(running) {
    const btnStart = document.getElementById("hme-btn-runner-start");
    const btnStop = document.getElementById("hme-btn-runner-stop");
    if (btnStart) btnStart.disabled = !!running;
    if (btnStop) btnStop.disabled = !running;
  }

  function setRunnerStats(cycle, stats) {
    const c = document.getElementById("hme-runner-stats-cycle");
    const cr = document.getElementById("hme-runner-stats-created");
    const er = document.getElementById("hme-runner-stats-errors");
    const sk = document.getElementById("hme-runner-stats-skipped");
    if (c) c.textContent = "#" + (cycle || 0);
    const s = stats || {};
    if (cr) cr.textContent = String(s.created || 0);
    if (er) er.textContent = String(s.errors || 0);
    if (sk) sk.textContent = String(s.skipped || 0);
  }

  // ── Countdown next_cycle_at (task 6.4 — R13.7) ─────────────────────
  // Khi RunStatus.next_cycle_at != null (Runner đang trong _interruptible_sleep
  // giữa 2 cycle), render countdown MM:SS cập nhật mỗi giây. Khi null (đang
  // trong cycle hoặc idle) → ẩn countdown + clear setInterval để tránh leak.
  //
  // Lifecycle:
  //   - pollRunnerStatus() (mỗi 2s) gọi setNextCycleCountdown(data.next_cycle_at).
  //   - setNextCycleCountdown null → clear timer + ẩn element.
  //   - setNextCycleCountdown ISO string → parse Date, lưu epoch ms target,
  //     start setInterval 1s tick → render MM:SS = (target - now) / 1000.
  //   - Khi delta <= 0 → render "00:00" (server sẽ flip next_cycle_at→null
  //     ngay khi vào cycle, poll kế tiếp tự ẩn).
  //   - Khi user rời tab HME → stopRunnerStatusPoll() được gọi, mà
  //     stopRunnerStatusPoll cũng phải tear down countdown để symmetric.
  let _nextCycleTargetMs = null;
  let _nextCycleTimer = null;

  function _renderCountdownTick() {
    const el = document.getElementById("hme-runner-next-cycle-countdown");
    if (!el) return;
    if (_nextCycleTargetMs == null) {
      el.style.display = "none";
      el.textContent = "";
      return;
    }
    const now = Date.now();
    let deltaSec = Math.max(0, Math.round((_nextCycleTargetMs - now) / 1000));
    const mm = Math.floor(deltaSec / 60);
    const ss = deltaSec % 60;
    el.textContent = "| Next cycle in " + pad2(mm) + ":" + pad2(ss);
    el.style.display = "";
  }

  function _clearNextCycleCountdown() {
    if (_nextCycleTimer != null) {
      clearInterval(_nextCycleTimer);
      _nextCycleTimer = null;
    }
    _nextCycleTargetMs = null;
    const el = document.getElementById("hme-runner-next-cycle-countdown");
    if (el) {
      el.style.display = "none";
      el.textContent = "";
    }
  }

  function setNextCycleCountdown(nextCycleIso) {
    if (!nextCycleIso) {
      _clearNextCycleCountdown();
      return;
    }
    const targetMs = Date.parse(nextCycleIso);
    if (isNaN(targetMs)) {
      // Không parse được → ẩn để khỏi hiển thị NaN.
      _clearNextCycleCountdown();
      return;
    }
    _nextCycleTargetMs = targetMs;
    // Tick ngay lập tức để khỏi chờ 1s render lần đầu.
    _renderCountdownTick();
    if (_nextCycleTimer == null) {
      _nextCycleTimer = setInterval(_renderCountdownTick, 1000);
    }
  }

  async function pollRunnerStatus() {
    try {
      const resp = await api("/api/icloud/run/status");
      if (!resp.ok) {
        // 401/500/... — không tự suy đoán state; giữ UI hiện tại + log warn.
        console.warn("[hme-runner] status fail:", resp.status);
        return;
      }
      const data = await resp.json();
      setRunnerStatusBadge(!!data.running);
      setRunnerButtons(!!data.running);
      setRunnerStats(data.cycle, data.stats);
      setNextCycleCountdown(data.next_cycle_at);
      updateProfilesRunnerIndicator(data);
      applyProfileStatesToCards(data);
    } catch (exc) {
      console.warn("[hme-runner] status err:", exc.message);
    }
  }

  function updateProfilesRunnerIndicator(data) {
    const dot = document.getElementById("hme-profiles-runner-dot");
    const text = document.getElementById("hme-profiles-runner-text");
    if (!dot || !text) return;

    dot.classList.remove("runner-dot-running", "runner-dot-waiting", "runner-dot-idle");

    if (!data.running) {
      dot.classList.add("runner-dot-idle");
      text.textContent = "Đã dừng";
      return;
    }
    if (data.next_cycle_at) {
      // Đang chờ giữa các cycle
      dot.classList.add("runner-dot-waiting");
      const remain = Math.max(0, Math.round((Date.parse(data.next_cycle_at) - Date.now()) / 1000));
      const mm = String(Math.floor(remain / 60)).padStart(2, "0");
      const ss = String(remain % 60).padStart(2, "0");
      text.textContent = `Chờ cycle tiếp (${mm}:${ss})`;
    } else {
      // Đang chạy trong cycle
      dot.classList.add("runner-dot-running");
      const s = data.stats || {};
      const cur = data.current_apple_id;
      const curMsg = cur ? ` · profile ${privacyMask(cur)}` : "";
      text.textContent = `Đang tạo - Cycle #${data.cycle || 0} | ${s.created || 0} created${curMsg}`;
    }
  }

  // ── Per-profile runtime badge (icloud-runner-loop revised) ──────────────
  // Map state literal → {label, css class}. Badge gắn vào span placeholder
  // .hme-profile-runtime-badge trong card. Bị clear khi runner idle hoặc
  // profile không xuất hiện trong profile_states (idle).
  const PROFILE_STATE_BADGE_MAP = {
    running:  { label: "RUNNING",  cls: "badge-active"  },
    waiting:  { label: "WAITING",  cls: "badge-warn"    },
    done:     { label: "DONE",     cls: "badge-success" },
    cooldown: { label: "COOLDOWN", cls: "badge-muted"   },
    disabled: { label: "DISABLED", cls: "badge-error"   },
    idle:     { label: "",         cls: ""              },
  };

  function applyProfileStatesToCards(data) {
    // Iterate mọi placeholder span, đổi nội dung theo data.profile_states.
    // Profile không có entry trong map → coi như idle (badge ẩn).
    const states = data.profile_states || {};
    const placeholders = document.querySelectorAll(".hme-profile-runtime-badge");
    if (!placeholders.length) return;
    const runnerActive = !!data.running && data.action === "generate";
    placeholders.forEach((node) => {
      const apple = node.dataset.appleId;
      const state = runnerActive ? (states[apple] || "idle") : "idle";
      const def = PROFILE_STATE_BADGE_MAP[state] || PROFILE_STATE_BADGE_MAP.idle;
      if (!def.label) {
        node.textContent = "";
        node.className = "hme-profile-runtime-badge";
        node.removeAttribute("title");
        return;
      }
      // Nếu profile đang RUNNING + có current_apple_id khớp → highlight
      // bằng class extra (CSS pulse) để dễ thấy giữa N card.
      const pulseCls =
        state === "running" && data.current_apple_id === apple
          ? " hme-runtime-pulse"
          : "";
      node.textContent = def.label;
      node.className = `badge ${def.cls} hme-profile-runtime-badge${pulseCls}`;
      node.title = `Runner state: ${state}`;
    });
  }

  function startRunnerStatusPoll() {
    if (_runnerStatusTimer) return;
    pollRunnerStatus(); // immediate first tick
    _runnerStatusTimer = setInterval(pollRunnerStatus, 2000);
  }

  function stopRunnerStatusPoll() {
    if (_runnerStatusTimer) {
      clearInterval(_runnerStatusTimer);
      _runnerStatusTimer = null;
    }
    // Tear down countdown song song với poll status — khi user rời tab HME
    // không còn nguồn cập nhật next_cycle_at nữa, giữ countdown chạy = leak.
    _clearNextCycleCountdown();
  }

  function readRunnerForm() {
    const action = document.getElementById("hme-runner-action").value;
    const countRaw = document.getElementById("hme-runner-count-per-cycle").value.trim();
    const retryRaw = document.getElementById("hme-runner-retry-interval").value.trim();
    const label = document.getElementById("hme-runner-label").value.trim();
    const note = document.getElementById("hme-runner-note").value.trim();
    const params = {};
    if (countRaw) {
      const n = parseInt(countRaw, 10);
      if (!Number.isNaN(n) && n > 0) {
        // Backend (HmeRunner._run_one_cycle action='generate') ưu tiên
        // count_per_profile; fallback count_per_cycle nếu thiếu.
        // Semantic mới: số email tối đa mỗi profile mỗi cycle.
        params.count_per_profile = n;
      }
    }
    if (label) params.label = label;
    if (note) params.note = note;
    const body = { action, params };
    if (retryRaw) {
      const s = parseInt(retryRaw, 10);
      if (!Number.isNaN(s) && s >= 10) body.retry_interval = s;
    }
    return body;
  }

  // ── Runner form config persist (auto-save + restore) ──────────────────
  // Backend là source of truth (file <runtime>/icloud/runner_config.json).
  // UI flow:
  //   - Vào tab HME → loadRunnerConfig() GET /run/config → fill form.
  //   - Mỗi field đổi → debounce 400ms → saveRunnerConfig() PUT /run/config.
  //   - Trước khi Start → flush save sync (await) để chắc state file khớp form.
  //
  // Chống vòng lặp: khi loadRunnerConfig() ghi vào input → trigger event
  // 'change' / 'input', listener sẽ gọi save lại. Dùng cờ _runnerConfigSuspend
  // để skip save khi đang fill form từ load.
  let _runnerConfigDebounce = null;
  let _runnerConfigSuspend = false;
  // True khi user đã chạm input MÀ KHÔNG phải do fillRunnerForm. Dùng để
  // loadRunnerConfig() bỏ qua overwrite nếu user đã gõ trong thời gian
  // GET /run/config chưa về (race tab-open vs response).
  let _runnerConfigUserDirty = false;

  function readRunnerFormConfig() {
    // Body khớp schema RunnerConfig (PUT /run/config). KHÁC readRunnerForm
    // (build POST /run body): action+params nested. Ở đây flat 5 key.
    const action = document.getElementById("hme-runner-action").value;
    const countRaw = document.getElementById("hme-runner-count-per-cycle").value.trim();
    const retryRaw = document.getElementById("hme-runner-retry-interval").value.trim();
    const label = document.getElementById("hme-runner-label").value.trim();
    const note = document.getElementById("hme-runner-note").value.trim();
    const cfg = { action: action || "generate" };
    if (countRaw) {
      const n = parseInt(countRaw, 10);
      cfg.count_per_cycle = !Number.isNaN(n) && n > 0 ? n : null;
    } else {
      cfg.count_per_cycle = null;
    }
    if (retryRaw) {
      const s = parseInt(retryRaw, 10);
      cfg.retry_interval = !Number.isNaN(s) && s >= 10 ? s : null;
    } else {
      cfg.retry_interval = null;
    }
    cfg.label = label || null;
    cfg.note = note || null;
    return cfg;
  }

  function fillRunnerForm(cfg) {
    // Gán lại vào input — set _runnerConfigSuspend để listener KHÔNG echo save.
    _runnerConfigSuspend = true;
    try {
      const sel = document.getElementById("hme-runner-action");
      if (sel) sel.value = cfg.action || "generate";
      const cnt = document.getElementById("hme-runner-count-per-cycle");
      if (cnt) cnt.value = cfg.count_per_cycle != null ? String(cfg.count_per_cycle) : "";
      const rty = document.getElementById("hme-runner-retry-interval");
      if (rty) rty.value = cfg.retry_interval != null ? String(cfg.retry_interval) : "";
      const lbl = document.getElementById("hme-runner-label");
      if (lbl) lbl.value = cfg.label || "";
      const nte = document.getElementById("hme-runner-note");
      if (nte) nte.value = cfg.note || "";
    } finally {
      // Dùng setTimeout để ensure các event sync (vd 'change') đã flush xong
      // trước khi tắt cờ — input.value = ... không trigger 'input' nhưng
      // <select>.value = ... có thể không phát 'change'; vẫn giữ guard cho
      // chắc.
      setTimeout(() => {
        _runnerConfigSuspend = false;
      }, 0);
    }
  }

  async function loadRunnerConfig() {
    // Mỗi lần vào tab HME → reset dirty flag để load lần này được phép fill
    // (user có thể chưa chạm input ở session tab hiện tại).
    _runnerConfigUserDirty = false;
    try {
      const resp = await api("/api/icloud/run/config");
      if (!resp.ok) {
        console.warn("[hme-runner-config] GET fail:", resp.status);
        return;
      }
      const cfg = await resp.json();
      // Race: nếu user đã gõ vào form trong khi GET chưa về, KHÔNG ghi đè
      // input của user (giá trị user gõ thắng — đã được auto-save trước
      // đó nên file backend đã update; load chỉ để khôi phục khi user
      // chưa làm gì).
      if (_runnerConfigUserDirty) {
        if (cfg._warning) {
          console.warn("[hme-runner-config]", cfg._warning);
        }
        return;
      }
      fillRunnerForm(cfg);
      if (cfg._warning) {
        console.warn("[hme-runner-config]", cfg._warning);
      }
    } catch (exc) {
      console.warn("[hme-runner-config] GET err:", exc.message);
    }
  }

  async function saveRunnerConfig() {
    if (_runnerConfigSuspend) return;
    try {
      const body = readRunnerFormConfig();
      const resp = await api("/api/icloud/run/config", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        console.warn(
          "[hme-runner-config] PUT fail:",
          resp.status,
          data.detail || data.error || ""
        );
      }
    } catch (exc) {
      console.warn("[hme-runner-config] PUT err:", exc.message);
    }
  }

  function scheduleRunnerConfigSave() {
    if (_runnerConfigSuspend) return;
    _runnerConfigUserDirty = true;
    if (_runnerConfigDebounce) clearTimeout(_runnerConfigDebounce);
    _runnerConfigDebounce = setTimeout(() => {
      _runnerConfigDebounce = null;
      saveRunnerConfig();
    }, 400);
  }

  function flushRunnerConfigSave() {
    // Dùng trước Start để chắc file đã reflect form hiện tại — tránh case
    // user gõ retry_interval xong bấm Start trong < 400ms, save debounce
    // chưa kịp fire.
    if (_runnerConfigDebounce) {
      clearTimeout(_runnerConfigDebounce);
      _runnerConfigDebounce = null;
    }
    return saveRunnerConfig();
  }

  async function clickRunnerStart() {
    const btn = document.getElementById("hme-btn-runner-start");
    if (btn) btn.disabled = true;
    try {
      // Flush pending debounce save trước khi Start — đảm bảo file
      // runner_config.json reflect form hiện tại, để backend rebuild
      // _runner với retry_interval đã lưu nếu sau đó server restart.
      await flushRunnerConfigSave();
      const body = readRunnerForm();
      const resp = await api("/api/icloud/run", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.status === 409) {
        await Dialog.alert({
          title: "Runner đang chạy",
          message: "Runner đang chạy action khác. Bấm Stop trước rồi thử lại.",
        });
        return;
      }
      if (!resp.ok) {
        await Dialog.alert({
          title: "Start fail",
          message: `HTTP ${resp.status}`,
          detail: data.detail || data.error || "",
        });
        return;
      }
      // Force refresh status ngay sau Start để badge / nút sync nhanh.
      pollRunnerStatus();
    } catch (exc) {
      await Dialog.alert({ title: "Network error", message: exc.message });
    } finally {
      // Để pollRunnerStatus quyết định disable cuối cùng dựa trên server state.
      pollRunnerStatus();
    }
  }

  async function clickRunnerStop() {
    const btn = document.getElementById("hme-btn-runner-stop");
    if (btn) btn.disabled = true;
    try {
      const resp = await api("/api/icloud/run/stop", { method: "POST" });
      if (!resp.ok) {
        await Dialog.alert({ title: "Stop fail", message: `HTTP ${resp.status}` });
      }
    } catch (exc) {
      await Dialog.alert({ title: "Network error", message: exc.message });
    } finally {
      pollRunnerStatus();
    }
  }

  // ── Emails page ───────────────────────────────────────────────────────
  let _selectedEmails = new Set();
  let _emailPage = 0;
  const _emailPageSize = 50;
  let _emailTotal = 0;

  function renderEmailPagination() {
    const container = document.getElementById("hme-email-pagination");
    if (!container) return;
    const totalPages = Math.max(1, Math.ceil(_emailTotal / _emailPageSize));
    const currentPage = _emailPage + 1;
    container.innerHTML = `
      <button class="btn btn-ghost btn-small" id="hme-email-prev" ${_emailPage <= 0 ? "disabled" : ""}>← Prev</button>
      <span class="muted">${currentPage} / ${totalPages} (${_emailTotal} emails)</span>
      <button class="btn btn-ghost btn-small" id="hme-email-next" ${currentPage >= totalPages ? "disabled" : ""}>Next →</button>
    `;
    document.getElementById("hme-email-prev")?.addEventListener("click", () => {
      if (_emailPage > 0) { _emailPage--; loadEmails(); }
    });
    document.getElementById("hme-email-next")?.addEventListener("click", () => {
      if (currentPage < totalPages) { _emailPage++; loadEmails(); }
    });
  }

  async function loadEmails() {
    const tbody = document.getElementById("hme-emails-tbody");
    const status = document.getElementById("hme-email-status-filter").value;
    const appleId = document.getElementById("hme-email-apple-id-filter").value;
    const label = document.getElementById("hme-email-label-filter").value;
    tbody.innerHTML = `<tr><td colspan="7" class="muted">Loading...</td></tr>`;
    try {
      const params = new URLSearchParams();
      if (status) params.set("status", status);
      if (appleId) params.set("apple_id", appleId);
      if (label) params.set("label", label);
      params.set("limit", String(_emailPageSize));
      params.set("offset", String(_emailPage * _emailPageSize));
      const resp = await api(`/api/icloud/emails?${params}`);
      if (!resp.ok) {
        renderEmpty(tbody, 7, `Error: ${resp.status}`);
        return;
      }
      const data = await resp.json();
      const rows = data.rows;
      _emailTotal = data.total;
      renderEmailPagination();
      if (!rows.length) {
        renderEmpty(tbody, 7, "No emails.");
        return;
      }
      tbody.innerHTML = rows
        .map((r) => {
          // RAW giữ trong value/data-* (API call); DISPLAY masked theo toggle.
          const emailRaw = escapeHtml(r.email);
          const emailDisp = escapeHtml(privacyMask(r.email));
          const appleDisp = escapeHtml(privacyMask(r.apple_id));
          return `
        <tr>
          <td><input type="checkbox" class="hme-email-select" value="${emailRaw}" /></td>
          <td><code title="${emailDisp}">${emailDisp}</code></td>
          <td><code title="${appleDisp}">${appleDisp}</code></td>
          <td>${statusBadge(r.status)}</td>
          <td>${escapeHtml(r.label || "-")}</td>
          <td>${escapeHtml(fmtDt(r.created_at))}</td>
          <td>
            <button class="btn btn-ghost btn-small hme-email-action" data-email="${emailRaw}" data-action="deactivate" title="Deactivate email">Off</button>
            <button class="btn btn-ghost btn-small hme-email-action" data-email="${emailRaw}" data-action="reactivate" title="Reactivate email">On</button>
            <button class="btn btn-ghost btn-small hme-email-action" data-email="${emailRaw}" data-action="delete" title="Delete email">Del</button>
          </td>
        </tr>`;
        })
        .join("");
      bindEmailActions();
      _selectedEmails.clear();
      updateBulkActionsBar();
    } catch (exc) {
      renderEmpty(tbody, 7, `Error: ${exc.message}`);
    }
  }

  function bindEmailActions() {
    document.querySelectorAll(".hme-email-action").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const email = btn.dataset.email;
        const emailDisp = privacyMask(email);
        const action = btn.dataset.action;
        const ans = await Dialog.choice({
          title: "Email action?",
          message: `${action} ${emailDisp}. Dry run chỉ preview; Run sẽ thực hiện thật.`,
          actions: [
            { label: "Dry run", value: "dry", className: "btn btn-ghost" },
            {
              label: "Run",
              value: "run",
              className: action === "delete" ? "btn btn-danger" : "btn btn-primary",
            },
          ],
        });
        if (!ans) {
          return; // abort
        }
        const dryRun = ans === "dry";
        const url =
          action === "delete"
            ? `/api/icloud/emails/${encodeURIComponent(email)}`
            : `/api/icloud/emails/${encodeURIComponent(email)}/${action}`;
        const resp = await api(
          url + (dryRun ? "?dry_run=true" : ""),
          { method: action === "delete" ? "DELETE" : "POST" }
        );
        const data = await resp.json();
        await Dialog.alert({
          title: "Email action result",
          message: "Server response:",
          detail: JSON.stringify(data, null, 2),
        });
        loadEmails();
      });
    });
    document.querySelectorAll(".hme-email-select").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) _selectedEmails.add(cb.value);
        else _selectedEmails.delete(cb.value);
        updateBulkActionsBar();
      });
    });
    document.getElementById("hme-email-select-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      document.querySelectorAll(".hme-email-select").forEach((cb) => {
        cb.checked = checked;
        if (checked) _selectedEmails.add(cb.value);
        else _selectedEmails.delete(cb.value);
      });
      updateBulkActionsBar();
    });
  }

  function updateBulkActionsBar() {
    const bar = document.getElementById("hme-bulk-actions");
    const count = _selectedEmails.size;
    if (count > 0) {
      bar.style.display = "flex";
      document.getElementById("hme-bulk-count").textContent = `${count} selected`;
    } else {
      bar.style.display = "none";
    }
  }

  async function bulkAction(action) {
    if (_selectedEmails.size === 0) return;
    const dryRun = document.getElementById("hme-bulk-dry-run").checked;
    const ok = await Dialog.confirm({
      title: "Bulk email action?",
      message: `${action} ${_selectedEmails.size} email(s). dry_run=${dryRun}`,
      confirmLabel: action,
      danger: action === "delete",
    });
    if (!ok) return;
    const resp = await api(`/api/icloud/emails/bulk/${action}`, {
      method: "POST",
      body: JSON.stringify({
        emails: Array.from(_selectedEmails),
        dry_run: dryRun,
      }),
    });
    const data = await resp.json();
    await Dialog.alert({
      title: "Bulk action result",
      message: "Server response:",
      detail: JSON.stringify(data, null, 2),
    });
    _selectedEmails.clear();
    loadEmails();
  }

  // ── Add Profile dialog (R14) ────────────────────────────────────────
  // Flow:
  //   1. User click `+ Thêm profile` → POST /add/start → backend launch
  //      Camoufox HEADED + return session_id.
  //   2. UI mở dialog, poll GET /add/<id>/status mỗi 2s để cập nhật state.
  //   3. User login Apple ID + 2FA tay trong Camoufox → click `Lưu` → POST
  //      /add/<id>/save → state saving → done. Hoặc click `Huỷ` → POST
  //      /add/<id>/cancel → state cancelling → cancelled.
  //   4. Watchdog timeout (server-side, 30 phút default) → state tự
  //      transition → cancelled với reason='profile_add_timeout'.
  //   5. Khi terminal (done|cancelled|failed): stop poll, đóng dialog hoặc
  //      hiện error inline tuỳ kết quả.

  let _addProfileState = {
    sessionId: null,        // null = không có dialog đang mở
    pollTimer: null,        // setInterval handle
    durationTimer: null,    // setInterval handle update duration display
    startedAt: null,        // Date object
    state: null,            // string state hiện tại
  };

  function openAddProfileDialog() {
    document.getElementById("hme-add-profile-modal").style.display = "flex";
  }

  function closeAddProfileDialog() {
    document.getElementById("hme-add-profile-modal").style.display = "none";
    stopAddProfilePolling();
    _addProfileState = {
      sessionId: null,
      pollTimer: null,
      durationTimer: null,
      startedAt: null,
      state: null,
    };
    resetAddProfileDialog();
  }

  function resetAddProfileDialog() {
    document.getElementById("hme-add-session-id").textContent = "-";
    document.getElementById("hme-add-state-badge").innerHTML =
      '<span class="badge badge-muted">-</span>';
    document.getElementById("hme-add-duration").textContent = "0s";
    document.getElementById("hme-add-timeout-hint").textContent = "";
    document.getElementById("hme-add-error-row").style.display = "none";
    document.getElementById("hme-add-error-text").textContent = "";
    document.getElementById("hme-add-save-btn").disabled = true;
    document.getElementById("hme-add-cancel-btn").disabled = false;
    const input = document.getElementById("hme-add-apple-id");
    if (input) {
      input.value = "";
      input.classList.remove("input-error");
    }
  }

  function stopAddProfilePolling() {
    if (_addProfileState.pollTimer) {
      clearInterval(_addProfileState.pollTimer);
      _addProfileState.pollTimer = null;
    }
    if (_addProfileState.durationTimer) {
      clearInterval(_addProfileState.durationTimer);
      _addProfileState.durationTimer = null;
    }
  }

  async function startAddProfile() {
    const btn = document.getElementById("hme-btn-add-profile");
    btn.disabled = true;
    btn.textContent = "Đang mở Camoufox...";
    try {
      const resp = await api("/api/icloud/profiles/add/start", {
        method: "POST",
      });
      const data = await resp.json();
      if (resp.status === 409) {
        await Dialog.alert({
          title: "Add profile đang chạy",
          message:
            `Đã có session Add_Profile khác đang chạy ` +
            `(session_id=${data.active_session_id}). ` +
            `Hoàn tất hoặc huỷ session đó trước.`,
        });
        return;
      }
      if (!resp.ok) {
        await Dialog.alert({
          title: "Lỗi mở Camoufox",
          message: `${data.error || resp.status}`,
          detail: data.message || "",
        });
        return;
      }
      _addProfileState.sessionId = data.session_id;
      _addProfileState.startedAt = new Date(data.started_at);
      _addProfileState.state = "recording";
      document.getElementById("hme-add-session-id").textContent =
        data.session_id.slice(0, 12) + "…";
      openAddProfileDialog();
      startPollingAddProfile(data.session_id);
    } catch (exc) {
      await Dialog.alert({ title: "Network error", message: exc.message });
    } finally {
      btn.disabled = false;
      btn.textContent = "+ Add";
    }
  }

  function startPollingAddProfile(sessionId) {
    // Poll status mỗi 2s.
    _addProfileState.pollTimer = setInterval(async () => {
      try {
        const resp = await api(
          `/api/icloud/profiles/add/${encodeURIComponent(sessionId)}/status`
        );
        if (resp.status === 404 || resp.status === 422) {
          // Session không tồn tại (evict khỏi cache, process restart, hoặc
          // session_id sai). Coi như terminal → đóng dialog.
          stopAddProfilePolling();
          _addProfileState.state = "cancelled";
          closeAddProfileDialog();
          return;
        }
        const data = await resp.json();
        updateAddProfileUI(data);
        if (["done", "cancelled", "failed"].includes(data.state)) {
          await handleAddProfileTerminal(data);
        }
      } catch (exc) {
        console.warn("[hme-add] poll fail:", exc);
      }
    }, 2000);
    // Duration counter mỗi 1s.
    _addProfileState.durationTimer = setInterval(() => {
      if (!_addProfileState.startedAt) return;
      const elapsed = Math.floor(
        (Date.now() - _addProfileState.startedAt.getTime()) / 1000
      );
      document.getElementById("hme-add-duration").textContent =
        elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;
    }, 1000);
  }

  function updateAddProfileUI(data) {
    _addProfileState.state = data.state;
    document.getElementById("hme-add-state-badge").innerHTML = statusBadge(data.state);
    document.getElementById("hme-add-error-row").style.display = data.error_reason
      ? "flex"
      : "none";
    document.getElementById("hme-add-error-text").textContent =
      data.error_reason ? `${data.error_reason}: ${data.error || ""}` : "";
    // Enable Lưu chỉ khi state = recording (R14: user phải login + 2FA xong).
    document.getElementById("hme-add-save-btn").disabled = data.state !== "recording";
    // Disable Huỷ khi đã ở terminal state.
    document.getElementById("hme-add-cancel-btn").disabled = ["done", "cancelled", "failed"].includes(
      data.state
    );
  }

  async function handleAddProfileTerminal(data) {
    stopAddProfilePolling();
    if (data.state === "done") {
      // Đóng modal silent — table refresh + status badge đổi là đủ feedback.
      // Không alert nữa để tránh user phải click OK thêm 1 lần (R14.x UX).
      closeAddProfileDialog();
      loadProfiles();
      loadPoolStatus();
    } else if (data.state === "cancelled") {
      // User chủ động Huỷ → đóng dialog im lặng. KHÔNG alert "Session đã mất"
      // dù error_reason là 'session_not_found' (race condition do poll late
      // hit cache evicted hoặc backend đã GC) — UI đã hiện inline error rồi.
      closeAddProfileDialog();
    } else if (data.state === "failed") {
      // Giữ dialog mở để user thấy error inline.
      // Nếu error là recoverable (apple_id_not_extractable, cookies_not_ready),
      // backend đã transition state→RECORDING — ta không bao giờ hit branch
      // này với reason đó. Nếu hit (terminal: apple_id_already_exists,
      // move_failed, ...) → disable cả 2 nút, ép user click X đóng tay.
      document.getElementById("hme-add-save-btn").disabled = true;
      document.getElementById("hme-add-cancel-btn").disabled = true;
    }
  }

  async function clickSaveAddProfile() {
    const sessionId = _addProfileState.sessionId;
    if (!sessionId) return;
    const btn = document.getElementById("hme-add-save-btn");
    const appleIdInput = document.getElementById("hme-add-apple-id");
    const appleId = appleIdInput ? appleIdInput.value.trim() : "";

    btn.disabled = true;
    btn.textContent = "Đang lưu...";
    try {
      // Body chỉ chứa apple_id khi user nhập — server fallback auto-extract
      // khi field rỗng/null.
      const body = appleId ? { apple_id: appleId } : {};
      const resp = await api(
        `/api/icloud/profiles/add/${encodeURIComponent(sessionId)}/save`,
        {
          method: "POST",
          body: JSON.stringify(body),
        }
      );
      const data = await resp.json();
      if (resp.ok) {
        // Success — handle terminal NGAY tại đây (R14.15) thay vì chờ poll
        // round kế tiếp. Race condition cũ: poll hit /status sau khi server
        // đã clear _active → 404 → UI hiểu nhầm "Session đã mất". Giờ cache
        // terminal ở backend đã fix race, nhưng vẫn nên handle response trực
        // tiếp để UI feedback nhanh (không chờ 2s poll interval).
        await handleAddProfileTerminal({
          state: "done",
          apple_id: data.apple_id,
          error: null,
          error_reason: null,
        });
        return;
      }
      // Map error: 400 / 409 / 500 — show inline error trong dialog, KHÔNG đóng.
      document.getElementById("hme-add-error-row").style.display = "flex";
      document.getElementById("hme-add-error-text").textContent =
        `${data.error || resp.status}: ${data.message || ""}`;
      // Re-enable Lưu để user retry (vd cookies_not_ready → user login xong rồi
      // bấm lại). 1 số reason terminal (apple_id_already_exists, move_failed)
      // sẽ tự transition state='failed' → poll catch + handler đóng dialog.
      btn.disabled = false;
      // Hint user cụ thể khi auto-extract fail: focus vào input apple_id.
      if (data.error === "apple_id_not_extractable" && appleIdInput) {
        appleIdInput.focus();
        appleIdInput.classList.add("input-error");
      }
      // Apple_id user nhập KHÁC account đã login trong Camoufox (A17).
      // Highlight input để user sửa hint hoặc clear input dùng auto-extract.
      if (data.error === "apple_id_mismatch" && appleIdInput) {
        appleIdInput.focus();
        appleIdInput.select();
        appleIdInput.classList.add("input-error");
      }
    } catch (exc) {
      document.getElementById("hme-add-error-row").style.display = "flex";
      document.getElementById("hme-add-error-text").textContent =
        `Network error: ${exc.message}`;
      btn.disabled = false;
    } finally {
      btn.textContent = "Lưu";
    }
  }

  async function clickCancelAddProfile() {
    const sessionId = _addProfileState.sessionId;
    if (!sessionId) {
      closeAddProfileDialog();
      return;
    }
    const btn = document.getElementById("hme-add-cancel-btn");
    btn.disabled = true;
    btn.textContent = "Đang huỷ...";
    try {
      const resp = await api(
        `/api/icloud/profiles/add/${encodeURIComponent(sessionId)}/cancel`,
        { method: "POST" }
      );
      if (!resp.ok) {
        // Session không tồn tại hoặc đã terminal → close dialog luôn.
        closeAddProfileDialog();
        return;
      }
      // poll sẽ tự bắt state='cancelled' và terminal handler đóng dialog.
    } catch (exc) {
      console.warn("[hme-add] cancel fail:", exc);
      closeAddProfileDialog();
    } finally {
      btn.textContent = "Huỷ";
    }
  }

  async function clickCloseAddProfile() {
    // Click nút X — nếu session còn live thì cảnh báo + giữ chạy nền.
    const sessionId = _addProfileState.sessionId;
    const state = _addProfileState.state;
    if (sessionId && !["done", "cancelled", "failed"].includes(state || "")) {
      const ok = await Dialog.confirm({
        title: "Đóng dialog?",
        message: "Đóng dialog sẽ giữ session đang chạy nền và Camoufox vẫn mở. Bạn vẫn muốn đóng?",
        confirmLabel: "Đóng dialog",
      });
      if (!ok) return;
    }
    closeAddProfileDialog();
  }

  // ── Open Profile dialog (R15) ──────────────────────────────────────
  // Flow:
  //   1. User click nút Open trên row → POST /profiles/<apple_id>/open/start
  //      → backend acquire write_lock + launch Camoufox HEADED.
  //   2. UI mở dialog, poll GET /status mỗi 2s.
  //   3. User check session / re-login + 2FA tay → click Lưu → POST /save.
  //      Hoặc click Đóng → POST /close (KHÔNG sửa DB).
  //   4. Watchdog timeout (server, 30 phút) → state tự transition → closed.
  //   5. Khi terminal:
  //      - saved   → refresh table + toast "Đã verify".
  //      - closed  → đóng dialog im lặng (không refresh, không toast).
  //      - failed  → giữ dialog hiện inline error, disable cả 2 nút.

  let _openProfileState = {
    sessionId: null,
    appleId: null,
    pollTimer: null,
    durationTimer: null,
    startedAt: null,
    state: null,
  };

  function openOpenProfileDialog() {
    document.getElementById("hme-open-profile-modal").style.display = "flex";
  }

  function closeOpenProfileDialog() {
    document.getElementById("hme-open-profile-modal").style.display = "none";
    stopOpenProfilePolling();
    _openProfileState = {
      sessionId: null,
      appleId: null,
      pollTimer: null,
      durationTimer: null,
      startedAt: null,
      state: null,
    };
    resetOpenProfileDialog();
    // Re-enable mọi nút Open trên table sau khi session terminal.
    document.querySelectorAll(".hme-profile-open").forEach((b) => {
      b.disabled = false;
      b.title = "Mở Camoufox HEADED để xem / login lại profile (R15)";
    });
  }

  function resetOpenProfileDialog() {
    document.getElementById("hme-open-modal-title").textContent = "Đang mở profile";
    document.getElementById("hme-open-apple-id").textContent = "-";
    document.getElementById("hme-open-session-id").textContent = "-";
    document.getElementById("hme-open-state-badge").innerHTML =
      '<span class="badge badge-muted">-</span>';
    document.getElementById("hme-open-previous-status").textContent = "-";
    document.getElementById("hme-open-duration").textContent = "0s";
    document.getElementById("hme-open-error-row").style.display = "none";
    document.getElementById("hme-open-error-text").textContent = "";
    document.getElementById("hme-open-save-btn").disabled = true;
    document.getElementById("hme-open-close-btn").disabled = false;
  }

  function stopOpenProfilePolling() {
    if (_openProfileState.pollTimer) {
      clearInterval(_openProfileState.pollTimer);
      _openProfileState.pollTimer = null;
    }
    if (_openProfileState.durationTimer) {
      clearInterval(_openProfileState.durationTimer);
      _openProfileState.durationTimer = null;
    }
  }

  async function startOpenProfile(appleId) {
    if (!appleId) return;
    if (_openProfileState.sessionId) {
      await Dialog.alert({
        title: "Open profile đang chạy",
        message:
          `Đã có Open_Profile session khác đang chạy ` +
          `(apple_id=${privacyMask(_openProfileState.appleId)}). Hoàn tất hoặc đóng session đó trước.`,
      });
      return;
    }
    // Disable mọi nút Open ngay (UI mirror Open_Profile_Lock_Single).
    document.querySelectorAll(".hme-profile-open").forEach((b) => {
      b.disabled = true;
      b.textContent = "Open...";
    });
    try {
      const resp = await api(
        `/api/icloud/profiles/${encodeURIComponent(appleId)}/open/start`,
        { method: "POST" }
      );
      const data = await resp.json();
      if (resp.status === 404) {
        await Dialog.alert({
          title: "Profile không tồn tại",
          message: data.message || privacyMask(appleId),
        });
        return;
      }
      if (resp.status === 409) {
        if (data.error === "profile_locked") {
          await Dialog.alert({
            title: "Profile đang bị lock",
            message:
              `Profile ${privacyMask(appleId)} đang được dùng bởi flow khác ` +
              `(bootstrap / recorder / open). Đợi flow đó hoàn tất.`,
          });
        } else {
          await Dialog.alert({
            title: "Open profile đang chạy",
            message:
              `Đã có Open_Profile session khác đang chạy ` +
              `(session_id=${data.active_session_id}, apple_id=${privacyMask(data.active_apple_id)}).`,
          });
        }
        return;
      }
      if (!resp.ok) {
        await Dialog.alert({
          title: "Lỗi mở Camoufox",
          message: `${data.error || resp.status}`,
          detail: data.message || "",
        });
        return;
      }
      _openProfileState.sessionId = data.session_id;
      _openProfileState.appleId = data.apple_id;
      _openProfileState.startedAt = new Date(data.started_at);
      _openProfileState.state = "opening";
      const appleDisp = privacyMask(data.apple_id);
      document.getElementById("hme-open-modal-title").textContent =
        `Đang mở profile ${appleDisp}`;
      document.getElementById("hme-open-apple-id").textContent = appleDisp;
      document.getElementById("hme-open-session-id").textContent =
        data.session_id.slice(0, 12) + "…";
      document.getElementById("hme-open-previous-status").textContent =
        data.previous_status || "-";
      openOpenProfileDialog();
      startPollingOpenProfile(data.session_id, data.apple_id);
    } catch (exc) {
      await Dialog.alert({ title: "Network error", message: exc.message });
    } finally {
      // Re-enable + đặt lại label nút (chỉ khi không vào state đã mở dialog).
      document.querySelectorAll(".hme-profile-open").forEach((b) => {
        b.textContent = "Open";
        if (!_openProfileState.sessionId) {
          b.disabled = false;
        }
      });
    }
  }

  function startPollingOpenProfile(sessionId, appleId) {
    _openProfileState.pollTimer = setInterval(async () => {
      try {
        const resp = await api(
          `/api/icloud/profiles/${encodeURIComponent(appleId)}/open/${encodeURIComponent(sessionId)}/status`
        );
        if (resp.status === 404 || resp.status === 422) {
          // Session không tồn tại / mismatch → đóng dialog.
          stopOpenProfilePolling();
          closeOpenProfileDialog();
          return;
        }
        const data = await resp.json();
        updateOpenProfileUI(data);
        if (["saved", "closed", "failed"].includes(data.state)) {
          await handleOpenProfileTerminal(data);
        }
      } catch (exc) {
        console.warn("[hme-open] poll fail:", exc);
      }
    }, 2000);
    _openProfileState.durationTimer = setInterval(() => {
      if (!_openProfileState.startedAt) return;
      const elapsed = Math.floor(
        (Date.now() - _openProfileState.startedAt.getTime()) / 1000
      );
      document.getElementById("hme-open-duration").textContent =
        elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;
    }, 1000);
  }

  function updateOpenProfileUI(data) {
    _openProfileState.state = data.state;
    document.getElementById("hme-open-state-badge").innerHTML = statusBadge(data.state);
    document.getElementById("hme-open-error-row").style.display = data.error_reason
      ? "flex"
      : "none";
    document.getElementById("hme-open-error-text").textContent = data.error_reason
      ? `${data.error_reason}: ${data.error || ""}`
      : "";
    if (data.previous_status) {
      document.getElementById("hme-open-previous-status").textContent =
        data.previous_status;
    }
    // Enable Lưu chỉ khi state='open' (đã chuẩn bị xong, chờ user thao tác).
    document.getElementById("hme-open-save-btn").disabled = data.state !== "open";
    // Disable Đóng khi đã ở terminal state.
    document.getElementById("hme-open-close-btn").disabled = ["saved", "closed", "failed"].includes(
      data.state
    );
  }

  async function handleOpenProfileTerminal(data) {
    stopOpenProfilePolling();
    if (data.state === "saved") {
      // Đóng modal silent — table refresh + status badge đổi là đủ feedback.
      // Không alert nữa để tránh user phải click OK thêm 1 lần (R15.x UX).
      closeOpenProfileDialog();
      loadProfiles();
      loadPoolStatus();
    } else if (data.state === "closed") {
      // User Close hoặc watchdog timeout — đóng dialog im lặng.
      // Không refresh table vì DB không đổi (R15.15).
      closeOpenProfileDialog();
    } else if (data.state === "failed") {
      // Giữ dialog mở để user thấy error inline. Disable cả 2 nút.
      document.getElementById("hme-open-save-btn").disabled = true;
      document.getElementById("hme-open-close-btn").disabled = true;
    }
  }

  async function clickSaveOpenProfile() {
    const sessionId = _openProfileState.sessionId;
    const appleId = _openProfileState.appleId;
    if (!sessionId || !appleId) return;
    const btn = document.getElementById("hme-open-save-btn");
    btn.disabled = true;
    btn.textContent = "Đang lưu...";
    try {
      const resp = await api(
        `/api/icloud/profiles/${encodeURIComponent(appleId)}/open/${encodeURIComponent(sessionId)}/save`,
        { method: "POST" }
      );
      const data = await resp.json();
      if (resp.ok) {
        // Success — handle terminal NGAY (không chờ poll round kế tiếp).
        await handleOpenProfileTerminal({
          state: "saved",
          apple_id: data.apple_id,
          previous_status: data.previous_status,
          error: null,
          error_reason: null,
        });
        return;
      }
      // 400 cookies_not_ready → giữ dialog mở, hiện inline error, re-enable nút.
      document.getElementById("hme-open-error-row").style.display = "flex";
      document.getElementById("hme-open-error-text").textContent =
        `${data.error || resp.status}: ${data.message || ""}`;
      btn.disabled = false;
    } catch (exc) {
      document.getElementById("hme-open-error-row").style.display = "flex";
      document.getElementById("hme-open-error-text").textContent =
        `Network error: ${exc.message}`;
      btn.disabled = false;
    } finally {
      btn.textContent = "Lưu";
    }
  }

  async function clickCloseOpenProfile() {
    const sessionId = _openProfileState.sessionId;
    const appleId = _openProfileState.appleId;
    if (!sessionId || !appleId) {
      closeOpenProfileDialog();
      return;
    }
    const btn = document.getElementById("hme-open-close-btn");
    btn.disabled = true;
    btn.textContent = "Đang đóng...";
    try {
      const resp = await api(
        `/api/icloud/profiles/${encodeURIComponent(appleId)}/open/${encodeURIComponent(sessionId)}/close`,
        { method: "POST" }
      );
      if (!resp.ok) {
        // Session không tồn tại / đã terminal → đóng dialog luôn.
        closeOpenProfileDialog();
        return;
      }
      // Poll sẽ bắt state='closed' và terminal handler đóng dialog.
    } catch (exc) {
      console.warn("[hme-open] close fail:", exc);
      closeOpenProfileDialog();
    } finally {
      btn.textContent = "Đóng";
    }
  }

  async function clickCloseOpenProfileX() {
    // Click nút X — nếu session còn live, cảnh báo + giữ chạy nền.
    const sessionId = _openProfileState.sessionId;
    const state = _openProfileState.state;
    if (sessionId && !["saved", "closed", "failed"].includes(state || "")) {
      const ok = await Dialog.confirm({
        title: "Đóng dialog?",
        message: "Đóng dialog sẽ giữ session chạy nền, Camoufox vẫn mở và lock vẫn giữ. Watchdog 30 phút sẽ tự đóng.",
        confirmLabel: "Đóng dialog",
      });
      if (!ok) return;
    }
    closeOpenProfileDialog();
  }

  // ── Init ──────────────────────────────────────────────────────────────
  function initBindings() {
    // Profiles panel
    document.getElementById("hme-btn-refresh-profiles")?.addEventListener("click", loadProfiles);
    document.getElementById("hme-btn-pool-status")?.addEventListener("click", loadPoolStatus);
    document.getElementById("hme-profile-status-filter")?.addEventListener("change", loadProfiles);
    document.getElementById("hme-btn-add-profile")?.addEventListener("click", startAddProfile);

    // Privacy mask toggle (anti screenshot leak) — chung cho cả 3 panel.
    document.getElementById("hme-btn-privacy")?.addEventListener("click", togglePrivacyMask);
    _updatePrivacyToggleBtn();

    // Run Log panel (task 6.1)
    document.getElementById("hme-btn-clear-log")?.addEventListener("click", clearLogPane);

    // Runner controls (task 6.2)
    document.getElementById("hme-btn-runner-start")?.addEventListener("click", clickRunnerStart);
    document.getElementById("hme-btn-runner-stop")?.addEventListener("click", clickRunnerStop);

    // Runner form auto-save (PUT /api/icloud/run/config debounced).
    // Cả input 'input' (text/number) lẫn select 'change' để cover mọi field.
    [
      "hme-runner-action",
      "hme-runner-count-per-cycle",
      "hme-runner-retry-interval",
      "hme-runner-label",
      "hme-runner-note",
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener("input", scheduleRunnerConfigSave);
      el.addEventListener("change", scheduleRunnerConfigSave);
    });

    // Emails panel
    document.getElementById("hme-btn-refresh-emails")?.addEventListener("click", loadEmails);
    document.getElementById("hme-email-status-filter")?.addEventListener("change", () => { _emailPage = 0; loadEmails(); });
    document.getElementById("hme-email-apple-id-filter")?.addEventListener("change", () => { _emailPage = 0; loadEmails(); });
    document.getElementById("hme-email-label-filter")?.addEventListener("change", () => { _emailPage = 0; loadEmails(); });

    // Add Profile modal (R14)
    document.getElementById("hme-add-modal-close")?.addEventListener("click", clickCloseAddProfile);
    document.getElementById("hme-add-save-btn")?.addEventListener("click", clickSaveAddProfile);
    document.getElementById("hme-add-cancel-btn")?.addEventListener("click", clickCancelAddProfile);

    // Open Profile modal (R15)
    document.getElementById("hme-open-modal-close")?.addEventListener("click", clickCloseOpenProfileX);
    document.getElementById("hme-open-save-btn")?.addEventListener("click", clickSaveOpenProfile);
    document.getElementById("hme-open-close-btn")?.addEventListener("click", clickCloseOpenProfile);

    // Bulk action bar
    document.querySelectorAll("#hme-bulk-actions [data-action]").forEach((btn) => {
      btn.addEventListener("click", () => bulkAction(btn.dataset.action));
    });

    // Auto-load 3 panel + pool status khi user click tab HME (sketch user:
    // Tab-aware: start polls when HME tab active, stop when leaving.
    // SSE log stream is always-on via SseBus (no per-tab connect/disconnect).
    document.addEventListener("gpt:tab", (e) => {
      if (e.detail.tab === "hme") {
        setTimeout(loadHmeDashboard, 50);
      } else {
        stopRunnerStatusPoll();
        stopProfileAutoRefresh();
      }
    });
  }

  // Expose loadEmails for cross-module use (autoreg.js refresh on success)
  window.loadHmeEmails = loadEmails;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initBindings);
  } else {
    initBindings();
  }
})();
