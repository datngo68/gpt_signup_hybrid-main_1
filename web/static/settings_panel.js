// settings_panel.js — Tab "Settings" với sidebar dọc.
// Section đầu: cấu hình proxy pool (repeater nhiều proxy URL để xoay vòng).
//
// Nguồn dữ liệu: backend Settings Store qua /api/proxy/pool (GET/POST) +
// /api/proxy/test-all. KHÔNG dùng localStorage cho config (theo project rules).
(function () {
  "use strict";

  // ── Auth helper (reuse pattern app.js/hme.js) ──────────────────────────
  function api(path, opts) {
    opts = opts || {};
    var token =
      (window.GptUi && window.GptUi.getAuthToken && window.GptUi.getAuthToken()) || "";
    var headers = Object.assign(
      { "Content-Type": "application/json" },
      token ? { "X-API-Token": token } : {},
      opts.headers || {}
    );
    return fetch(path, Object.assign({}, opts, { headers: headers })).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error("HTTP " + r.status + ": " + t);
        });
      }
      return r.json();
    });
  }

  var $ = function (id) { return document.getElementById(id); };

  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Mask credential khi hiển thị trạng thái (user:pass@host → ***@host)
  function maskProxy(url) {
    if (!url) return "direct";
    var m = String(url).match(/^([a-z][a-z0-9+.-]*):\/\/([^@/]+)@(.+)$/i);
    return m ? m[1] + "://***@" + m[3] : url;
  }

  // ── State ──────────────────────────────────────────────────────────────
  var state = {
    rows: [],          // [{id, value}] — danh sách proxy đang edit
    mode: "round_robin",
    lastResults: null, // map proxy → {ok, public_ip, detail}
    loaded: false,
    busy: false,
  };
  var _rowSeq = 0;

  var dom = {};

  function cacheDom() {
    dom.section = $("settings-section-proxies");
    dom.rowsHost = $("proxy-pool-rows");
    dom.modeSelect = $("proxy-pool-mode");
    dom.summary = $("proxy-pool-summary");
    dom.btnAdd = $("proxy-pool-add");
    dom.btnPaste = $("proxy-pool-paste");
    dom.btnTestAll = $("proxy-pool-test-all");
    dom.btnSave = $("proxy-pool-save");
    dom.statusLine = $("proxy-pool-status");
    dom.loginFlow = $("login-flow-select");
    // Paste modal
    dom.pasteModal = $("proxy-paste-modal");
    dom.pasteTextarea = $("proxy-paste-textarea");
    dom.pasteClose = $("proxy-paste-close");
    dom.pasteCancel = $("proxy-paste-cancel");
    dom.pasteApply = $("proxy-paste-apply");
    // Sidebar
    dom.navItems = Array.prototype.slice.call(
      document.querySelectorAll("#tab-settings .settings-nav-item")
    );
    dom.panes = Array.prototype.slice.call(
      document.querySelectorAll("#tab-settings [data-settings-pane]")
    );
    // Telegram section
    dom.tgBotToken = $("telegram-bot-token");
    dom.tgChatId = $("telegram-chat-id");
    dom.tgSave = $("telegram-save");
    dom.tgTest = $("telegram-test");
    dom.tgStatus = $("telegram-status");
    dom.tgBadge = $("telegram-status-badge");
    // Tunnel section
    dom.tunToggle = $("tunnel-enabled-toggle");
    dom.tunToggleLabel = $("tunnel-toggle-label");
    dom.tunBadge = $("tunnel-status-badge");
    dom.tunUrlInput = $("tunnel-url-input");
    dom.tunUrlCopy = $("tunnel-url-copy");
    dom.tunUrlOpen = $("tunnel-url-open");
    dom.tunRestart = $("tunnel-restart");
    dom.tunStatus = $("tunnel-status-line");
    dom.tunLogTail = $("tunnel-log-tail");
  }

  // ── Sidebar section switching ────────────────────────────────────────────
  function activateSection(sectionId) {
    dom.navItems.forEach(function (btn) {
      var on = btn.dataset.settingsSection === sectionId;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    dom.panes.forEach(function (pane) {
      pane.classList.toggle("active", pane.dataset.settingsPane === sectionId);
    });
  }

  // ── Row rendering ──────────────────────────────────────────────────────
  function makeRow(value) {
    return { id: "pp-" + _rowSeq++, value: value || "" };
  }

  function renderRows() {
    if (state.rows.length === 0) {
      dom.rowsHost.innerHTML =
        '<div class="proxy-pool-empty muted">Chưa có proxy nào. Bấm "Thêm proxy" hoặc "Dán hàng loạt".</div>';
      updateSummary();
      return;
    }
    var html = state.rows
      .map(function (row, idx) {
        var res = state.lastResults ? state.lastResults[row.value.trim()] : null;
        var dotCls = "proxy-dot";
        var statusTxt = "";
        if (res) {
          if (res.ok) {
            dotCls = "proxy-dot proxy-dot-ok";
            statusTxt = res.public_ip ? "IP " + escHtml(res.public_ip) : "live";
          } else {
            dotCls = "proxy-dot proxy-dot-fail";
            statusTxt = "dead";
          }
        }
        return (
          '<div class="proxy-pool-row" data-row-id="' + row.id + '">' +
            '<span class="proxy-pool-index">' + (idx + 1) + "</span>" +
            '<span class="' + dotCls + '" title="' + escHtml(statusTxt || "chưa test") + '"></span>' +
            '<input type="text" class="proxy-pool-input" data-row-id="' + row.id + '"' +
              ' value="' + escHtml(row.value) + '"' +
              ' placeholder="http://user:pass@host:port" spellcheck="false" autocomplete="off" />' +
            '<span class="proxy-pool-row-status">' + escHtml(statusTxt) + "</span>" +
            '<button class="icon-btn icon-danger proxy-pool-remove" data-row-id="' + row.id +
              '" type="button" title="Xoá" aria-label="Xoá proxy">' +
              (window.GptUi ? window.GptUi.icon("remove") : "×") +
            "</button>" +
          "</div>"
        );
      })
      .join("");
    dom.rowsHost.innerHTML = html;
    updateSummary();
  }

  function updateSummary() {
    var total = state.rows.filter(function (r) { return r.value.trim(); }).length;
    var live = 0;
    var dead = 0;
    if (state.lastResults) {
      state.rows.forEach(function (r) {
        var res = state.lastResults[r.value.trim()];
        if (res) { res.ok ? live++ : dead++; }
      });
    }
    var txt = total + " proxy";
    if (state.lastResults) txt += " · " + live + " live · " + dead + " dead";
    dom.summary.textContent = txt;
    dom.summary.className = "badge " + (dead > 0 ? "badge-warn" : (live > 0 ? "badge-success" : "badge-muted"));
  }

  // Sync giá trị từ input DOM về state (trước khi save/test)
  function syncRowsFromDom() {
    var inputs = dom.rowsHost.querySelectorAll(".proxy-pool-input");
    Array.prototype.forEach.call(inputs, function (inp) {
      var row = state.rows.find(function (r) { return r.id === inp.dataset.rowId; });
      if (row) row.value = inp.value;
    });
  }

  function collectProxies() {
    syncRowsFromDom();
    var seen = {};
    var out = [];
    state.rows.forEach(function (r) {
      var v = r.value.trim();
      if (v && !seen[v]) { seen[v] = 1; out.push(v); }
    });
    return out;
  }

  function setStatus(text, kind) {
    dom.statusLine.textContent = text || "";
    dom.statusLine.className = "proxy-pool-status muted" + (kind ? " proxy-pool-status-" + kind : "");
  }

  // ── Load from backend ──────────────────────────────────────────────────
  function load() {
    loadLoginFlow();
    return api("/api/proxy/pool")
      .then(function (data) {
        state.mode = data.rotation_mode || "round_robin";
        dom.modeSelect.value = state.mode;
        var proxies = data.proxies || [];
        state.rows = proxies.map(function (p) { return makeRow(p); });
        if (state.rows.length === 0) state.rows.push(makeRow(""));
        state.loaded = true;
        renderRows();
        var rt = data.runtime || {};
        if (rt.total) {
          setStatus("Đã lưu " + rt.total + " proxy · " + (rt.live || 0) + " live.", null);
        }
      })
      .catch(function (err) {
        setStatus("Load thất bại: " + err.message, "fail");
      });
  }

  // ── Save ───────────────────────────────────────────────────────────────
  function save() {
    if (state.busy) return;
    var proxies = collectProxies();
    state.busy = true;
    dom.btnSave.disabled = true;
    setStatus("Đang lưu…", null);
    api("/api/proxy/pool", {
      method: "POST",
      body: JSON.stringify({ proxies: proxies, rotation_mode: dom.modeSelect.value }),
    })
      .then(function (data) {
        state.mode = data.rotation_mode;
        // Normalize lại danh sách theo backend (đã dedupe)
        state.rows = (data.proxies || []).map(function (p) { return makeRow(p); });
        if (state.rows.length === 0) state.rows.push(makeRow(""));
        state.lastResults = null;
        renderRows();
        var extra = data.settings_persist_error ? " (cảnh báo: " + data.settings_persist_error + ")" : "";
        setStatus("Đã lưu " + proxies.length + " proxy." + extra, data.settings_persist_error ? "fail" : "ok");
      })
      .catch(function (err) {
        setStatus("Lưu thất bại: " + err.message, "fail");
      })
      .finally(function () {
        state.busy = false;
        dom.btnSave.disabled = false;
      });
  }

  // ── Test All ─────────────────────────────────────────────────────────────
  function testAll() {
    if (state.busy) return;
    var proxies = collectProxies();
    if (proxies.length === 0) {
      setStatus("Không có proxy để test.", "fail");
      return;
    }
    state.busy = true;
    dom.btnTestAll.disabled = true;
    setStatus("Đang test " + proxies.length + " proxy…", null);
    api("/api/proxy/test-all", {
      method: "POST",
      body: JSON.stringify({ proxies: proxies }),
    })
      .then(function (data) {
        var map = {};
        (data.results || []).forEach(function (item) {
          map[item.proxy] = item;
        });
        state.lastResults = map;
        renderRows();
        setStatus(
          "Test xong: " + (data.live || 0) + " live / " + (data.dead || 0) + " dead / " + (data.total || 0) + " tổng.",
          (data.dead || 0) > 0 ? "fail" : "ok"
        );
      })
      .catch(function (err) {
        setStatus("Test thất bại: " + err.message, "fail");
      })
      .finally(function () {
        state.busy = false;
        dom.btnTestAll.disabled = false;
      });
  }

  // ── Telegram section ─────────────────────────────────────────────────
  var tgState = { loaded: false, busy: false };

  function setTgStatus(text, kind) {
    if (!dom.tgStatus) return;
    dom.tgStatus.textContent = text || "";
    dom.tgStatus.className = "proxy-pool-status muted" + (kind ? " proxy-pool-status-" + kind : "");
  }

  function setTgBadge(configured) {
    if (!dom.tgBadge) return;
    dom.tgBadge.textContent = configured ? "đã cấu hình" : "chưa cấu hình";
    dom.tgBadge.className = "badge " + (configured ? "badge-success" : "badge-muted");
  }

  // ── Login flow (session.login_flow — setting toàn cục) ──────────────────
  function loadLoginFlow() {
    if (!dom.loginFlow) return Promise.resolve();
    return api("/api/settings/session.login_flow")
      .then(function (data) {
        dom.loginFlow.value = (data && data.value === "legacy") ? "legacy" : "anti409";
      })
      .catch(function () {
        dom.loginFlow.value = "anti409";  // 404/chưa set → default
      });
  }

  function saveLoginFlow() {
    if (!dom.loginFlow) return;
    var val = dom.loginFlow.value === "legacy" ? "legacy" : "anti409";
    api("/api/settings/session.login_flow", {
      method: "PUT",
      body: JSON.stringify({ value: val }),
    })
      .then(function () {
        setStatus("Login flow: " + val, "ok");
      })
      .catch(function (err) {
        setStatus("Lưu login flow thất bại: " + err.message, "fail");
      });
  }

  function loadTelegram() {
    if (!dom.tgBotToken) return Promise.resolve();
    return api("/api/telegram/config")
      .then(function (data) {
        dom.tgBotToken.value = data.bot_token || "";
        dom.tgChatId.value = data.chat_id || "";
        setTgBadge(!!data.configured);
        tgState.loaded = true;
      })
      .catch(function (err) {
        setTgStatus("Load thất bại: " + err.message, "fail");
      });
  }

  // ── Cloudflare Tunnel section ────────────────────────────────────────
  var tunState = { loaded: false, busy: false, polling: null };

  function setTunStatusLine(text, kind) {
    if (!dom.tunStatus) return;
    dom.tunStatus.textContent = text || "";
    dom.tunStatus.className = "proxy-pool-status muted" + (kind ? " proxy-pool-status-" + kind : "");
  }

  function renderTunnelSnapshot(snap) {
    if (!dom.tunBadge) return;
    var status = snap.status || "stopped";
    var enabled = !!snap.enabled;
    var url = snap.url || "";

    var badgeMap = {
      stopped: ["badge-muted", "stopped"],
      starting: ["badge-warn", "starting"],
      running: ["badge-success", "running"],
      failed: ["badge-danger", "failed"],
    };
    var bm = badgeMap[status] || badgeMap.stopped;
    dom.tunBadge.className = "badge " + bm[0];
    dom.tunBadge.textContent = bm[1];

    // Toggle (chỉ update nếu user không đang thao tác).
    if (!tunState.busy) {
      dom.tunToggle.checked = enabled;
    }
    dom.tunToggleLabel.textContent = enabled ? "Đang bật" : "Đang tắt";

    dom.tunUrlInput.value = url;
    dom.tunUrlInput.placeholder = enabled
      ? (status === "starting" ? "Đang xin URL từ Cloudflare…" : "(chưa có URL)")
      : "(tunnel đang tắt)";
    dom.tunUrlCopy.disabled = !url;
    dom.tunUrlOpen.disabled = !url;
    dom.tunRestart.disabled = !enabled || tunState.busy;

    if (snap.error) {
      setTunStatusLine("Lỗi: " + snap.error, "fail");
    } else if (status === "running" && snap.uptime_sec != null) {
      setTunStatusLine("Đang chạy · uptime " + snap.uptime_sec + "s · trỏ về " +
        (snap.local_host + ":" + snap.local_port), "ok");
    } else if (status === "starting") {
      setTunStatusLine("Đang khởi động cloudflared…", null);
    } else if (status === "stopped") {
      setTunStatusLine("Tunnel đã tắt.", null);
    }

    if (dom.tunLogTail) {
      var lines = (snap.log_tail || []).join("\n");
      dom.tunLogTail.textContent = lines || "(chưa có log)";
    }
  }

  function refreshTunnelStatus() {
    return api("/api/tunnel/status")
      .then(function (snap) {
        tunState.loaded = true;
        renderTunnelSnapshot(snap);
        return snap;
      })
      .catch(function (err) {
        setTunStatusLine("Load tunnel status thất bại: " + err.message, "fail");
      });
  }

  function ensureTunnelPolling(active) {
    if (active) {
      if (tunState.polling) return;
      tunState.polling = setInterval(function () {
        var tabActive = document.getElementById("tab-settings").classList.contains("active");
        var paneEl = document.querySelector('[data-settings-pane="tunnel"]');
        var paneActive = paneEl && paneEl.classList.contains("active");
        if (!tabActive || !paneActive) return;
        refreshTunnelStatus();
      }, 3000);
    } else {
      if (tunState.polling) {
        clearInterval(tunState.polling);
        tunState.polling = null;
      }
    }
  }

  function toggleTunnel() {
    if (tunState.busy) return;
    var want = !!dom.tunToggle.checked;
    tunState.busy = true;
    dom.tunToggle.disabled = true;
    setTunStatusLine(want ? "Đang bật tunnel (lần đầu sẽ tải cloudflared)…" : "Đang tắt tunnel…", null);
    api("/api/tunnel/config", {
      method: "POST",
      body: JSON.stringify({ enabled: want }),
    })
      .then(function (snap) {
        renderTunnelSnapshot(snap);
        if (snap.settings_persist_error) {
          setTunStatusLine("Đã áp dụng nhưng lưu DB lỗi: " + snap.settings_persist_error, "fail");
        }
      })
      .catch(function (err) {
        setTunStatusLine("Bật/tắt thất bại: " + err.message, "fail");
        dom.tunToggle.checked = !want;
      })
      .finally(function () {
        tunState.busy = false;
        dom.tunToggle.disabled = false;
      });
  }

  function restartTunnel() {
    if (tunState.busy) return;
    tunState.busy = true;
    dom.tunRestart.disabled = true;
    setTunStatusLine("Đang xin URL mới…", null);
    api("/api/tunnel/restart", { method: "POST" })
      .then(function (snap) {
        renderTunnelSnapshot(snap);
        if (snap.status === "running") {
          setTunStatusLine("Đã cấp URL mới.", "ok");
        }
      })
      .catch(function (err) {
        setTunStatusLine("Restart thất bại: " + err.message, "fail");
      })
      .finally(function () {
        tunState.busy = false;
        dom.tunRestart.disabled = false;
      });
  }

  function copyTunnelUrl() {
    var url = dom.tunUrlInput.value;
    if (!url) return;
    var done = function () { setTunStatusLine("Đã copy URL.", "ok"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(function () {
        dom.tunUrlInput.select();
        document.execCommand("copy");
        done();
      });
    } else {
      dom.tunUrlInput.select();
      document.execCommand("copy");
      done();
    }
  }

  function openTunnelUrl() {
    var url = dom.tunUrlInput.value;
    if (!url) return;
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function saveTelegram() {
    if (tgState.busy) return;
    tgState.busy = true;
    dom.tgSave.disabled = true;
    setTgStatus("Đang lưu…", null);
    api("/api/telegram/config", {
      method: "POST",
      body: JSON.stringify({
        bot_token: dom.tgBotToken.value.trim(),
        chat_id: dom.tgChatId.value.trim(),
      }),
    })
      .then(function (data) {
        setTgBadge(!!data.configured);
        var extra = data.persist_error ? " (cảnh báo: " + data.persist_error + ")" : "";
        setTgStatus("Đã lưu." + extra, data.persist_error ? "fail" : "ok");
      })
      .catch(function (err) {
        setTgStatus("Lưu thất bại: " + err.message, "fail");
      })
      .finally(function () {
        tgState.busy = false;
        dom.tgSave.disabled = false;
      });
  }

  function testTelegram() {
    if (tgState.busy) return;
    tgState.busy = true;
    dom.tgTest.disabled = true;
    setTgStatus("Đang gửi test…", null);
    // Lưu trước rồi test để dùng giá trị mới nhất.
    api("/api/telegram/config", {
      method: "POST",
      body: JSON.stringify({
        bot_token: dom.tgBotToken.value.trim(),
        chat_id: dom.tgChatId.value.trim(),
      }),
    })
      .then(function () { return api("/api/telegram/test", { method: "POST" }); })
      .then(function () { setTgStatus("Đã gửi tin test — kiểm tra Telegram.", "ok"); })
      .catch(function (err) { setTgStatus("Test thất bại: " + err.message, "fail"); })
      .finally(function () {
        tgState.busy = false;
        dom.tgTest.disabled = false;
      });
  }

  // ── Paste modal ──────────────────────────────────────────────────────────
  function openPaste() {
    dom.pasteTextarea.value = "";
    dom.pasteModal.style.display = "flex";
    dom.pasteTextarea.focus();
  }
  function closePaste() {
    dom.pasteModal.style.display = "none";
  }
  function applyPaste() {
    var lines = dom.pasteTextarea.value.split("\n");
    syncRowsFromDom();
    var existing = {};
    state.rows.forEach(function (r) {
      var v = r.value.trim();
      if (v) existing[v] = 1;
    });
    // Bỏ row rỗng cuối nếu đang trống
    state.rows = state.rows.filter(function (r) { return r.value.trim(); });
    var added = 0;
    lines.forEach(function (line) {
      var v = line.trim();
      if (v && !existing[v]) {
        existing[v] = 1;
        state.rows.push(makeRow(v));
        added++;
      }
    });
    if (state.rows.length === 0) state.rows.push(makeRow(""));
    state.lastResults = null;
    renderRows();
    closePaste();
    setStatus("Đã thêm " + added + " proxy. Nhớ bấm Lưu.", null);
  }

  // ── Event wiring ───────────────────────────────────────────────────────
  function bindEvents() {
    dom.navItems.forEach(function (btn) {
      btn.addEventListener("click", function () {
        activateSection(btn.dataset.settingsSection);
        if (btn.dataset.settingsSection === "tunnel") {
          refreshTunnelStatus();
        }
      });
    });

    dom.btnAdd.addEventListener("click", function () {
      syncRowsFromDom();
      state.rows.push(makeRow(""));
      renderRows();
      // Focus input vừa thêm
      var inputs = dom.rowsHost.querySelectorAll(".proxy-pool-input");
      if (inputs.length) inputs[inputs.length - 1].focus();
    });

    dom.btnPaste.addEventListener("click", openPaste);
    dom.btnTestAll.addEventListener("click", testAll);
    dom.btnSave.addEventListener("click", save);

    if (dom.tgSave) dom.tgSave.addEventListener("click", saveTelegram);
    if (dom.tgTest) dom.tgTest.addEventListener("click", testTelegram);
    if (dom.loginFlow) dom.loginFlow.addEventListener("change", saveLoginFlow);

    if (dom.tunToggle) dom.tunToggle.addEventListener("change", toggleTunnel);
    if (dom.tunRestart) dom.tunRestart.addEventListener("click", restartTunnel);
    if (dom.tunUrlCopy) dom.tunUrlCopy.addEventListener("click", copyTunnelUrl);
    if (dom.tunUrlOpen) dom.tunUrlOpen.addEventListener("click", openTunnelUrl);

    dom.modeSelect.addEventListener("change", function () {
      state.mode = dom.modeSelect.value;
    });

    // Delegation: remove row + input edit invalidate test result
    dom.rowsHost.addEventListener("click", function (e) {
      var btn = e.target.closest(".proxy-pool-remove");
      if (!btn) return;
      syncRowsFromDom();
      state.rows = state.rows.filter(function (r) { return r.id !== btn.dataset.rowId; });
      if (state.rows.length === 0) state.rows.push(makeRow(""));
      renderRows();
    });

    dom.rowsHost.addEventListener("input", function (e) {
      var inp = e.target.closest(".proxy-pool-input");
      if (!inp) return;
      var row = state.rows.find(function (r) { return r.id === inp.dataset.rowId; });
      if (row) row.value = inp.value;
    });

    // Paste modal
    dom.pasteClose.addEventListener("click", closePaste);
    dom.pasteCancel.addEventListener("click", closePaste);
    dom.pasteApply.addEventListener("click", applyPaste);
    dom.pasteModal.addEventListener("click", function (e) {
      if (e.target === dom.pasteModal) closePaste();
    });
  }

  // ── Lazy-load khi mở tab Settings lần đầu ────────────────────────────────
  function init() {
    cacheDom();
    if (!dom.section) return; // tab không tồn tại
    bindEvents();
    activateSection("proxies");

    document.addEventListener("gpt:tab", function (e) {
      if (e.detail && e.detail.tab === "settings" && !state.loaded) {
        load();
      }
      if (e.detail && e.detail.tab === "settings" && !tgState.loaded) {
        loadTelegram();
      }
      if (e.detail && e.detail.tab === "settings" && !tunState.loaded) {
        refreshTunnelStatus();
      }
      if (e.detail && e.detail.tab === "settings") {
        ensureTunnelPolling(true);
      } else {
        ensureTunnelPolling(false);
      }
    });

    // Nếu tab settings đã active sẵn lúc reload (ui.active_tab persisted)
    if (document.getElementById("tab-settings").classList.contains("active") && !state.loaded) {
      load();
      loadTelegram();
      refreshTunnelStatus();
      ensureTunnelPolling(true);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
