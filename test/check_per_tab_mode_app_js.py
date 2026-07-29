"""Static check pattern trong web/static/app.js cho per-tab mode refactor.

Verify:
    - Bỏ clamp Math.min(raw, 5)
    - MODE_TAB_CONFIG đúng default + cap mỗi tab
    - Listener save Settings key `<tab>.mode`
    - Event listener gpt:tab gọi _applyTabMode
    - _renderModeOptionsForTab filter theo cap
    - Không còn hydrate hardcode reg.mode ở bootstrap

Run: python3 test/check_per_tab_mode_app_js.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web/static/app.js"


def tc(idx: int, total: int, name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{tag}] [{idx}/{total}] {name}{suffix}", flush=True)
    return ok


def main() -> int:
    src = APP_JS.read_text(encoding="utf-8")

    cases: list[tuple[str, bool, str]] = []

    # 1. Không còn clamp Math.min(raw, 5)
    has_old_clamp = bool(re.search(r"Math\.min\(\s*raw\s*,\s*5\s*\)", src))
    cases.append(("TC-01 bỏ clamp Math.min(raw, 5)", not has_old_clamp,
                  "still has clamp" if has_old_clamp else "removed"))

    # 2. MODE_TAB_CONFIG đúng default + cap (search trực tiếp trong src,
    # tránh match nested object)
    has_reg = bool(re.search(
        r"reg\s*:\s*\{\s*defaultMode:\s*'multi10'\s*,\s*cap:\s*30\s*\}", src,
    ))
    has_session = bool(re.search(
        r"session\s*:\s*\{\s*defaultMode:\s*'multi10'\s*,\s*cap:\s*30\s*\}", src,
    ))
    has_upi = bool(re.search(
        r"upi\s*:\s*\{\s*defaultMode:\s*'multi30'\s*,\s*cap:\s*200\s*\}", src,
    ))
    cases.append(("TC-02 MODE_TAB_CONFIG.reg = {default:multi10, cap:30}", has_reg, ""))
    cases.append(("TC-03 MODE_TAB_CONFIG.session = {default:multi10, cap:30}", has_session, ""))
    cases.append(("TC-04 MODE_TAB_CONFIG.upi = {default:multi30, cap:200}", has_upi, ""))

    # 3. Listener save Settings key `<tab>.mode`
    has_per_tab_save = bool(re.search(
        r"Settings\.save\(\s*`\$\{tabId\}\.mode`\s*,", src,
    ))
    cases.append(("TC-05 listener save Settings.save(`${tabId}.mode`, ...)",
                  has_per_tab_save, ""))

    # 4. Event listener gpt:tab gọi _applyTabMode
    has_tab_event = bool(re.search(
        r"document\.addEventListener\(\s*'gpt:tab'.*?_applyTabMode",
        src, re.DOTALL,
    ))
    cases.append(("TC-06 listener 'gpt:tab' gọi _applyTabMode", has_tab_event, ""))

    # 5. _renderModeOptionsForTab dùng cap filter
    has_cap_filter = bool(re.search(
        r"_renderModeOptionsForTab.*?_ALL_MODE_OPTIONS\s*\.filter\(\s*o\s*=>\s*o\.n\s*<=\s*cfg\.cap",
        src, re.DOTALL,
    ))
    cases.append(("TC-07 render filter theo cfg.cap", has_cap_filter, ""))

    # 6. Bootstrap không còn hydrate hardcode reg.mode → dom.modeSelect.value
    # Cụ thể: dòng `dom.modeSelect.value = state.mode;` ở bootstrap đã xoá.
    has_old_hydrate = bool(re.search(
        r"const\s+mode\s*=\s*Settings\.get\('reg\.mode'\);\s*\n\s*if\s*\(mode\)\s*state\.mode",
        src,
    ))
    cases.append(("TC-08 bỏ hydrate hardcode reg.mode trong bootstrap",
                  not has_old_hydrate,
                  "still has old hydrate" if has_old_hydrate else "removed"))

    # 7. Reg-only sync server config trong listener change
    has_reg_only_sync = bool(re.search(
        r"if\s*\(\s*tabId\s*===\s*'reg'\s*\)\s*\{[^}]*api\(\s*'/api/config'",
        src, re.DOTALL,
    ))
    cases.append(("TC-09 listener Reg-only sync /api/config", has_reg_only_sync, ""))

    # 8. _ALL_MODE_OPTIONS có 10 options
    options_match = re.search(
        r"_ALL_MODE_OPTIONS\s*=\s*Object\.freeze\(\s*\[(.*?)\]\s*\)",
        src, re.DOTALL,
    )
    if options_match:
        n_opts = options_match.group(1).count("value:")
        cases.append((f"TC-10 _ALL_MODE_OPTIONS có 10 options (got {n_opts})",
                      n_opts == 10, ""))
    else:
        cases.append(("TC-10 _ALL_MODE_OPTIONS không tìm thấy", False, ""))

    total = len(cases)
    passed = sum(1 for i, (name, ok, detail) in enumerate(cases, 1)
                 if tc(i, total, name, ok, detail))
    print(f"=== Summary: {passed}/{total} PASS ===", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
