/**
 * Auto Reg GPT — sub-tab JS (auto-reg-gpt spec, tasks 5.2).
 *
 * Features:
 *   - Toggle START/STOP → POST /api/icloud/autoreg/start | /stop
 *   - SSE stream: via unified SseBus (autoreg_log channel)
 *   - Status polling: GET /api/icloud/autoreg/status every 3s when running
 *   - Left panel: emails table (status=created + used_for_chatgpt)
 *   - Right panel: append SSE events as lines (email|password|secret_2fa)
 *   - Copy output button
 */

(function () {
  "use strict";

  // ── Auth helper (reuse GptUi.getAuthToken pattern) ─────────────────
  function api(path, opts) {
    opts = opts || {};
    var token =
      (window.GptUi && window.GptUi.getAuthToken && window.GptUi.getAuthToken()) || "";
    var headers = Object.assign(
      { "Content-Type": "application/json" },
      token ? { "X-API-Token": token } : {},
      opts.headers || {}
    );
    return fetch(path, Object.assign({}, opts, { headers: headers }));
  }

  // ── DOM refs ───────────────────────────────────────────────────────
  var toggleBtn = document.getElementById("autoreg-toggle");
  var statusBadge = document.getElementById("autoreg-status-badge");
  var concurrencyInput = document.getElementById("autoreg-concurrency");
  var pollIntervalInput = document.getElementById("autoreg-poll-interval");
  var outputPane = document.getElementById("autoreg-output-pane");
  var copyBtn = document.getElementById("autoreg-copy-output");
  var statsCycle = document.getElementById("autoreg-stats-cycle");
  var statsProcessed = document.getElementById("autoreg-stats-processed");
  var statsSuccess = document.getElementById("autoreg-stats-success");
  var statsErrors = document.getElementById("autoreg-stats-errors");

  // ── State ──────────────────────────────────────────────────────────
  var _running = false;
  var _stopping = false;
  var _statusTimer = null;

  // ── Config hydration from Settings store ────────────────────────────
  function loadConfig() {
    // Hydrate form from unified Settings store (loaded via settings.js bootstrap)
    if (!window.Settings) return;
    var concurrency = Settings.get("autoreg.concurrency");
    var pollInterval = Settings.get("autoreg.poll_interval");
    if (concurrencyInput && concurrency != null) concurrencyInput.value = String(concurrency);
    if (pollIntervalInput && pollInterval != null) pollIntervalInput.value = String(pollInterval);
  }

  // ── Helpers ────────────────────────────────────────────────────────
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtDt(iso) {
    if (!iso) return "-";
    return iso.replace("T", " ").replace(/\.\d+Z?$/, "");
  }

  function pad2(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  function fmtLogTs(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // ── Toggle button logic ────────────────────────────────────────────
  function setRunningState(running) {
    _running = running;
    if (toggleBtn) {
      toggleBtn.textContent = running ? "Stop" : "Start";
      toggleBtn.classList.toggle("btn-primary", !running);
      toggleBtn.classList.toggle("btn-danger", running);
    }
    if (statusBadge) {
      statusBadge.textContent = running ? "RUNNING" : "IDLE";
      statusBadge.classList.remove("badge-active", "badge-muted");
      statusBadge.classList.add(running ? "badge-active" : "badge-muted");
    }
  }

  async function handleToggle() {
    if (_running) {
      // STOP — set _stopping flag to prevent pollStatus from flipping state back
      _stopping = true;
      setRunningState(false);
      stopStatusPoll();
      try {
        await api("/api/icloud/autoreg/stop", { method: "POST" });
      } catch (e) {
        console.error("[autoreg] stop error:", e);
      }
      // Poll until backend confirms stopped (max 30s)
      var maxWait = 30;
      var waited = 0;
      while (waited < maxWait) {
        try {
          var r = await api("/api/icloud/autoreg/status");
          if (r.ok) {
            var d = await r.json();
            if (!d.running) break;
          }
        } catch (e) { /* ignore */ }
        await new Promise(function (resolve) { setTimeout(resolve, 1000); });
        waited++;
      }
      _stopping = false;
    } else {
      // START — validate
      var concurrency = parseInt((concurrencyInput && concurrencyInput.value) || "1", 10);
      if (concurrency < 1) concurrency = 1;
      if (concurrency > 5) concurrency = 5;
      var pollInterval = parseInt((pollIntervalInput && pollIntervalInput.value) || "30", 10);
      if (pollInterval < 10) pollInterval = 10;

      var body = {
        concurrency: concurrency,
        poll_interval: pollInterval,
        logs_url: "",
        api_key: ""
      };

      try {
        var resp = await api("/api/icloud/autoreg/start", {
          method: "POST",
          body: JSON.stringify(body)
        });
        if (!resp.ok) {
          var errData = await resp.json().catch(function () { return {}; });
          await Dialog.alert({ message: "Start failed: " + (errData.error || errData.detail || resp.statusText) });
          return;
        }
      } catch (e) {
        console.error("[autoreg] start error:", e);
        await Dialog.alert({ message: "Start request failed: " + e.message });
        return;
      }

      setRunningState(true);
      clearOutput();
      startStatusPoll();
    }
  }

  if (toggleBtn) {
    toggleBtn.addEventListener("click", handleToggle);
  }

  // ── SSE via unified SseBus (autoreg_log channel) ────────────────────
  SseBus.on('autoreg_log', function (evt) {
    handleSSEEvent(evt);
  });

  function handleSSEEvent(evt) {
    var level = (evt && evt.level) || "info";
    var message = (evt && evt.message) || "";
    var ts = fmtLogTs(evt && evt.ts);
    var prefix = ts ? "[" + ts + "] " : "";

    if (level === "success") {
      // Format: email|password|secret_2fa
      var payload = evt.payload || {};
      var line = (payload.email || "") + "|" + (payload.password || "") + "|" + (payload.secret_2fa || "");
      appendOutput(prefix + line, "success");
    } else if (level === "error") {
      appendOutput(prefix + "[ERROR] " + message, "error");
    } else {
      appendOutput(prefix + message, level);
    }

    // Refresh emails on success events (use HME tab's loadEmails if available)
    if (level === "success" && typeof window.loadHmeEmails === "function") {
      window.loadHmeEmails();
    }
  }

  // ── Output pane ────────────────────────────────────────────────────
  function clearOutput() {
    if (outputPane) outputPane.textContent = "";
  }

  function appendOutput(text, level) {
    if (!outputPane) return;
    var line = document.createElement("div");
    line.className = "log-line";
    if (level === "error") line.className += " log-line-error";
    else if (level === "warn") line.className += " log-line-warn";
    else if (level === "success") line.className += " log-line-success";
    else line.className += " log-line-info";
    line.textContent = text;
    outputPane.appendChild(line);
    outputPane.scrollTop = outputPane.scrollHeight;
  }

  // ── Copy output ────────────────────────────────────────────────────
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      if (!outputPane) return;
      var text = outputPane.textContent || "";
      if (!text.trim()) return;
      navigator.clipboard.writeText(text).then(function () {
        copyBtn.textContent = "Copied!";
        setTimeout(function () { copyBtn.textContent = "Copy"; }, 1500);
      });
    });
  }

  // ── Status polling ─────────────────────────────────────────────────
  function startStatusPoll() {
    stopStatusPoll();
    pollStatus(); // immediate
    _statusTimer = setInterval(pollStatus, 3000);
  }

  function stopStatusPoll() {
    if (_statusTimer) {
      clearInterval(_statusTimer);
      _statusTimer = null;
    }
  }

  async function pollStatus() {
    if (_stopping) return; // Don't poll while stop is in progress
    try {
      var resp = await api("/api/icloud/autoreg/status");
      if (!resp.ok) return;
      if (_stopping) return; // Double check after await
      var data = await resp.json();
      setRunningState(!!data.running);
      if (statsCycle) statsCycle.textContent = "#" + (data.current_cycle || 0);
      if (statsProcessed) statsProcessed.textContent = String(data.processed || 0);
      if (statsSuccess) statsSuccess.textContent = String(data.success || 0);
      if (statsErrors) statsErrors.textContent = String(data.errors || 0);

      // If server says stopped, stop poll
      if (!data.running && _running) {
        setRunningState(false);
        stopStatusPoll();
      }
    } catch (e) {
      // Silently ignore poll errors
    }
  }

  // ── Init: check status on page load ────────────────────────────────
  async function initAutoReg() {
    loadConfig();
    try {
      var resp = await api("/api/icloud/autoreg/status");
      if (resp.ok) {
        var data = await resp.json();
        if (data.running) {
          setRunningState(true);
          startStatusPoll();
        }
        if (statsCycle) statsCycle.textContent = "#" + (data.current_cycle || 0);
        if (statsProcessed) statsProcessed.textContent = String(data.processed || 0);
        if (statsSuccess) statsSuccess.textContent = String(data.success || 0);
        if (statsErrors) statsErrors.textContent = String(data.errors || 0);
      }
    } catch (e) {
      // Endpoint not available yet — silent
    }
  }

  // Run init when HME tab becomes visible or on DOMContentLoaded
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAutoReg);
  } else {
    initAutoReg();
  }
})();
