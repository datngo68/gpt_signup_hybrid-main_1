/* web/static/dialog.js
 *
 * Unified modal dialogs — IIFE expose `window.Dialog`.
 *
 * Public API (chỉ ba method, không export gì khác):
 *   Dialog.alert({ title?, message, detail? })                 -> Promise<true>
 *   Dialog.confirm({ title?, message, detail?,                 -> Promise<boolean>
 *                    confirmLabel?, cancelLabel?, danger? })
 *   Dialog.choice({ title?, message, detail?,                  -> Promise<value | null>
 *                   actions: [{label, value, className?,
 *                              autofocus?}], cancelLabel? })
 *
 * Tái sử dụng nguyên DOM `#hme-feedback-modal` của index.html:
 *   #hme-feedback-modal / -title / -close / -message / -detail / -actions
 *
 * Quy ước:
 *   - alert  cancelValue = true   (Esc / × / mở dialog mới  -> resolve true)
 *   - confirm cancelValue = false (Esc / × / mở dialog mới  -> resolve false)
 *   - choice  cancelValue = null  (Esc / × / mở dialog mới  -> resolve null)
 *
 * Singleton: tại mọi thời điểm chỉ có ≤ 1 modal đang mở. Khi mở dialog mới
 * trong khi dialog cũ chưa đóng, dialog cũ được resolve ngay với cancelValue
 * tương ứng rồi mới mở dialog mới.
 *
 * Fail-fast (Req 4.4 + 6.4): KHÔNG đăng ký fallback im lặng cho
 * `window.Dialog`. Nếu module chưa nạp, caller sẽ throw `TypeError` tự nhiên
 * khi truy cập thuộc tính của `undefined`.
 */
(function () {
  "use strict";

  // ── Internal state ────────────────────────────────────────────────
  let _currentResolve = null;       // function | null
  let _currentCancelValue = null;   // any (true | false | null)
  let _lastFocus = null;            // Element | null
  let _keyHandler = null;           // function | null
  let _defaultButton = null;        // HTMLButtonElement | null

  // ── DOM contract ──────────────────────────────────────────────────
  // Lấy đúng 6 node theo id; throw đồng bộ nếu thiếu bất kỳ node nào.
  function _resolveNodes() {
    const ids = [
      "hme-feedback-modal",
      "hme-feedback-title",
      "hme-feedback-close",
      "hme-feedback-message",
      "hme-feedback-detail",
      "hme-feedback-actions"
    ];
    const found = {};
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      const node = document.getElementById(id);
      if (!node) {
        throw new Error("Dialog modal DOM contract violated: missing #" + id);
      }
      found[id] = node;
    }
    return {
      modal: found["hme-feedback-modal"],
      title: found["hme-feedback-title"],
      close: found["hme-feedback-close"],
      message: found["hme-feedback-message"],
      detail: found["hme-feedback-detail"],
      actions: found["hme-feedback-actions"]
    };
  }

  // ── Validation (đồng bộ, fail-fast) ───────────────────────────────
  function _validate(opts, opts2) {
    if (typeof opts !== "object" || opts === null) {
      throw new TypeError("Dialog: opts must be a non-null object");
    }
    if (typeof opts.message !== "string" || opts.message.length === 0) {
      throw new TypeError("Dialog: opts.message must be a non-empty string");
    }
    const optionalStringKeys = ["title", "detail", "confirmLabel", "cancelLabel"];
    for (let i = 0; i < optionalStringKeys.length; i++) {
      const key = optionalStringKeys[i];
      if (opts[key] !== undefined && typeof opts[key] !== "string") {
        throw new TypeError("Dialog: opts." + key + " must be a string");
      }
    }
    if (opts2 && opts2.requireActions === true) {
      if (!Array.isArray(opts.actions) || opts.actions.length === 0) {
        throw new Error("Dialog.choice: opts.actions must be a non-empty array");
      }
      for (let j = 0; j < opts.actions.length; j++) {
        const a = opts.actions[j];
        if (typeof a !== "object" || a === null) {
          throw new Error(
            "Dialog.choice: opts.actions[" + j + "] must be a non-null object"
          );
        }
        if (typeof a.label !== "string" || a.label.length === 0) {
          throw new Error(
            "Dialog.choice: opts.actions[" + j + "].label must be a non-empty string"
          );
        }
        // a.value: any (kể cả null/undefined đều OK)
      }
    }
  }

  // ── Render actions ────────────────────────────────────────────────
  // Reset innerHTML, dựng nút Cancel (nếu withCancel) + nút theo từng action,
  // gắn onclick → _close(value). Trả về defaultButton.
  function _renderActions(actionsNode, actions, withCancel, cancelLabel) {
    actionsNode.innerHTML = "";

    if (withCancel) {
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn btn-ghost";
      cancelBtn.textContent = cancelLabel;
      cancelBtn.onclick = function () {
        _close(_currentCancelValue);
      };
      actionsNode.appendChild(cancelBtn);
    }

    let defaultButton = null;
    let firstButton = null;

    for (let i = 0; i < actions.length; i++) {
      const action = actions[i];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = (typeof action.className === "string" && action.className.length > 0)
        ? action.className
        : "btn btn-ghost";
      btn.textContent = action.label;
      const value = action.value;
      btn.onclick = function () {
        _close(value);
      };
      actionsNode.appendChild(btn);

      if (firstButton === null) firstButton = btn;
      if (defaultButton === null && action.autofocus === true) {
        defaultButton = btn;
      }
    }

    if (defaultButton === null) defaultButton = firstButton;
    return defaultButton;
  }

  // ── Keydown handler (capture phase) ───────────────────────────────
  function _onKeyDown(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      _close(_currentCancelValue);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      let modal = null;
      try {
        modal = _resolveNodes().modal;
      } catch (err) {
        // DOM đã bị phá; không có gì để click.
        return;
      }
      const active = document.activeElement;
      if (
        active &&
        active.tagName === "BUTTON" &&
        modal.contains(active)
      ) {
        active.click();
      } else if (_defaultButton) {
        _defaultButton.click();
      }
    }
  }

  // ── Open dialog ───────────────────────────────────────────────────
  // - validate DOM (throw nếu thiếu node)
  // - singleton: đóng dialog cũ trước
  // - lưu _lastFocus
  // - set _currentResolve + _currentCancelValue ngay trong executor đồng bộ
  // - render title/message/detail/actions
  // - gắn keydown listener capture phase
  // - hiện modal, focus defaultButton
  function _open(opts) {
    const nodes = _resolveNodes();

    if (_currentResolve) {
      _close(_currentCancelValue);
    }

    _lastFocus = document.activeElement;

    return new Promise(function (resolve) {
      _currentResolve = resolve;
      _currentCancelValue = opts.cancelValue;

      const titleText = (typeof opts.title === "string" && opts.title.length > 0)
        ? opts.title
        : opts.defaultTitle;
      nodes.title.textContent = titleText;
      nodes.message.textContent = opts.message;

      if (typeof opts.detail === "string" && opts.detail.length > 0) {
        nodes.detail.textContent = opts.detail;
        nodes.detail.style.display = "block";
      } else {
        nodes.detail.textContent = "";
        nodes.detail.style.display = "none";
      }

      _defaultButton = _renderActions(
        nodes.actions,
        opts.actions,
        opts.withCancel,
        opts.cancelLabel
      );

      nodes.close.onclick = function () {
        _close(_currentCancelValue);
      };

      _keyHandler = _onKeyDown;
      document.addEventListener("keydown", _keyHandler, true);

      nodes.modal.style.display = "flex";

      if (_defaultButton && typeof _defaultButton.focus === "function") {
        _defaultButton.focus();
      }
    });
  }

  // ── Close dialog ──────────────────────────────────────────────────
  // Ẩn modal, gỡ keydown listener, resolve một lần, clear state, restore focus.
  function _close(value) {
    const modal = document.getElementById("hme-feedback-modal");
    if (modal) {
      modal.style.display = "none";
    }

    if (_keyHandler) {
      document.removeEventListener("keydown", _keyHandler, true);
      _keyHandler = null;
    }

    const resolve = _currentResolve;
    if (resolve) {
      resolve(value);
    }

    _currentResolve = null;
    _currentCancelValue = null;
    _defaultButton = null;

    const lastFocus = _lastFocus;
    _lastFocus = null;
    if (
      lastFocus &&
      typeof lastFocus.focus === "function" &&
      document.contains(lastFocus)
    ) {
      lastFocus.focus();
    }
  }

  // ── Public: Dialog.alert ──────────────────────────────────────────
  function alert(opts) {
    _validate(opts);
    return _open({
      title: opts.title,
      message: opts.message,
      detail: opts.detail,
      defaultTitle: "Thông báo",
      cancelValue: true,
      withCancel: false,
      cancelLabel: "",
      actions: [
        {
          label: "OK",
          value: true,
          className: "btn btn-primary",
          autofocus: true
        }
      ]
    });
  }

  // ── Public: Dialog.confirm ────────────────────────────────────────
  function confirm(opts) {
    _validate(opts);
    const danger = opts.danger === true;
    const confirmLabel = (typeof opts.confirmLabel === "string" && opts.confirmLabel.length > 0)
      ? opts.confirmLabel
      : "Tiếp tục";
    const cancelLabel = (typeof opts.cancelLabel === "string" && opts.cancelLabel.length > 0)
      ? opts.cancelLabel
      : "Huỷ";
    return _open({
      title: opts.title,
      message: opts.message,
      detail: opts.detail,
      defaultTitle: "Xác nhận",
      cancelValue: false,
      withCancel: true,
      cancelLabel: cancelLabel,
      actions: [
        {
          label: confirmLabel,
          value: true,
          className: danger ? "btn btn-danger" : "btn btn-primary",
          autofocus: true
        }
      ]
    });
  }

  // ── Public: Dialog.choice ─────────────────────────────────────────
  function choice(opts) {
    _validate(opts, { requireActions: true });
    const cancelLabel = (typeof opts.cancelLabel === "string" && opts.cancelLabel.length > 0)
      ? opts.cancelLabel
      : "Huỷ";
    const normalizedActions = [];
    for (let i = 0; i < opts.actions.length; i++) {
      const a = opts.actions[i];
      normalizedActions.push({
        label: a.label,
        value: a.value,
        className: (typeof a.className === "string" && a.className.length > 0)
          ? a.className
          : "btn btn-ghost",
        autofocus: a.autofocus === true
      });
    }
    return _open({
      title: opts.title,
      message: opts.message,
      detail: opts.detail,
      defaultTitle: "Thông báo",
      cancelValue: null,
      withCancel: true,
      cancelLabel: cancelLabel,
      actions: normalizedActions
    });
  }

  // ── Expose ────────────────────────────────────────────────────────
  window.Dialog = { alert: alert, confirm: confirm, choice: choice };
})();
