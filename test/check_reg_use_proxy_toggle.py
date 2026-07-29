"""Smoke check: Reg toggle use_proxy — Settings + Manager + Server + UI wiring."""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse(p: Path) -> None:
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def main() -> int:
    failures: list[str] = []

    # 1. Syntax check
    for f in [ROOT / "web" / "server.py", ROOT / "web" / "manager.py",
              ROOT / "db" / "repositories.py"]:
        try:
            parse(f)
            print(f"[PASS] syntax {f.relative_to(ROOT)}", flush=True)
        except SyntaxError as e:
            failures.append(f"syntax {f}: {e}")
            print(f"[FAIL] syntax {f.relative_to(ROOT)} :: {e}", flush=True)

    # 2. Settings validate reg.use_proxy accept bool, reject str/int
    from db.repositories import _validate_type_constraint, RepositoryError, _EXACT_KEYS

    if "reg.use_proxy" in _EXACT_KEYS:
        print(f"[PASS] reg.use_proxy in _EXACT_KEYS whitelist", flush=True)
    else:
        failures.append("reg.use_proxy missing from _EXACT_KEYS")
        print(f"[FAIL] reg.use_proxy missing from _EXACT_KEYS", flush=True)

    for v in (True, False):
        try:
            _validate_type_constraint("reg.use_proxy", v)
            print(f"[PASS] settings accept reg.use_proxy={v}", flush=True)
        except RepositoryError as e:
            failures.append(f"settings reject {v}: {e}")
            print(f"[FAIL] settings reject {v} :: {e}", flush=True)

    for v in ("yes", 1, 0, None):
        try:
            _validate_type_constraint("reg.use_proxy", v)
            failures.append(f"settings ACCEPT non-bool {v!r}")
            print(f"[FAIL] settings ACCEPT non-bool {v!r}", flush=True)
        except RepositoryError as e:
            print(f"[PASS] settings reject non-bool {v!r} :: {e.cause}", flush=True)

    # 3. JobManager use_proxy property + setter + apply_settings
    from web.manager import JobManager

    async def _check_jm() -> list[str]:
        local: list[str] = []
        jm = JobManager(max_concurrent=1)
        if jm.use_proxy is True:
            print(f"[PASS] JobManager default use_proxy=True", flush=True)
        else:
            local.append(f"default use_proxy={jm.use_proxy} (expected True)")

        jm.set_use_proxy(False)
        if jm.use_proxy is False:
            print(f"[PASS] JobManager.set_use_proxy(False) ok", flush=True)
        else:
            local.append(f"set_use_proxy False → {jm.use_proxy}")

        jm.set_use_proxy(True)
        if jm.use_proxy is True:
            print(f"[PASS] JobManager.set_use_proxy(True) ok", flush=True)
        else:
            local.append(f"set_use_proxy True → {jm.use_proxy}")

        # apply_settings hydrate
        jm.apply_settings({"reg.use_proxy": False})
        if jm.use_proxy is False:
            print(f"[PASS] apply_settings reg.use_proxy=False hydrated", flush=True)
        else:
            local.append(f"apply_settings False → {jm.use_proxy}")

        # 4. _resolve_proxy_for_job khi tắt → return (None, None) + set fields
        class _FakeJob:
            email = "test@x.com"

        log_lines: list[str] = []
        job = _FakeJob()
        url, line = await jm._resolve_proxy_for_job(job, lambda m: log_lines.append(m))
        if url is None and line is None:
            print(f"[PASS] _resolve_proxy_for_job(use_proxy=False) → (None, None)", flush=True)
        else:
            local.append(f"resolve gate → ({url}, {line}) expected (None, None)")
        if getattr(job, "_active_proxy", "MISS") is None and getattr(job, "_active_proxy_line", "MISS") is None:
            print(f"[PASS] transient fields cleared on gate", flush=True)
        else:
            local.append("transient fields not cleared")
        if any("[proxy] disabled" in m for m in log_lines):
            print(f"[PASS] gate logs '[proxy] disabled'", flush=True)
        else:
            local.append("gate did not log disabled message")
        return local

    failures.extend(asyncio.run(_check_jm()))

    # 5. Server SetConfigRequest + write-through key
    server_src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    if "use_proxy: bool | None" in server_src:
        print(f"[PASS] server.py SetConfigRequest.use_proxy field", flush=True)
    else:
        failures.append("server.py missing use_proxy in SetConfigRequest")

    if 'settings_dict["reg.use_proxy"] = payload.use_proxy' in server_src:
        print(f"[PASS] server.py write-through reg.use_proxy", flush=True)
    else:
        failures.append("server.py missing write-through reg.use_proxy")

    if 'manager.set_use_proxy(payload.use_proxy)' in server_src:
        print(f"[PASS] server.py calls manager.set_use_proxy", flush=True)
    else:
        failures.append("server.py missing manager.set_use_proxy call")

    if '"use_proxy": manager.use_proxy' in server_src:
        print(f"[PASS] server.py snapshot/response includes use_proxy", flush=True)
    else:
        failures.append("server.py response missing use_proxy")

    # 6. UI wiring
    html_src = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    if 'id="proxy-toggle"' in html_src:
        print(f"[PASS] index.html proxy-toggle present", flush=True)
    else:
        failures.append("index.html missing proxy-toggle")

    js_src = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for needle in (
        "proxyToggle: $('proxy-toggle')",
        "dom.proxyToggle.addEventListener('change'",
        "JSON.stringify({ use_proxy: useProxy })",
        "Settings.get('reg.use_proxy')",
        "data.use_proxy",
    ):
        if needle in js_src:
            print(f"[PASS] app.js contains: {needle}", flush=True)
        else:
            failures.append(f"app.js missing: {needle}")
            print(f"[FAIL] app.js missing: {needle}", flush=True)

    print("", flush=True)
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===", flush=True)
        for x in failures:
            print(f"  - {x}", flush=True)
        return 1
    print("=== ALL PASS ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
