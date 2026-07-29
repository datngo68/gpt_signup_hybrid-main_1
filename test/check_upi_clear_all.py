"""Smoke check: UPI clear-all — Manager + endpoint + UI wiring."""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse(p: Path) -> None:
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def main() -> int:
    failures: list[str] = []

    # 1. Syntax check
    for f in [ROOT / "web" / "server.py", ROOT / "web" / "manager.py"]:
        try:
            parse(f)
            print(f"[PASS] syntax {f.relative_to(ROOT)}", flush=True)
        except SyntaxError as e:
            failures.append(f"syntax {f}: {e}")
            print(f"[FAIL] syntax {f.relative_to(ROOT)} :: {e}", flush=True)

    # 2. UpiJobManager.clear_all xoá hết jobs + broadcast event
    from web.manager import UpiJobManager

    async def _check_mgr() -> list[str]:
        local: list[str] = []
        um = UpiJobManager(max_concurrent=1)

        # Inject 3 fake jobs trực tiếp (bypass add_jobs vì cần worker loop)
        for i in range(3):
            jid = f"j{i}"
            job = SimpleNamespace(
                id=jid,
                email=f"u{i}@x.com",
                status="success" if i < 2 else "queued",
                qr_path=None,
                finished_at=None,
            )
            um.jobs[jid] = job
            um.order.append(jid)

        # Capture broadcasts
        broadcasted: list[dict] = []
        um._broadcast = lambda ev: broadcasted.append(ev)  # type: ignore[assignment]

        removed = await um.clear_all()
        if removed == 3:
            print(f"[PASS] clear_all returned 3 (removed={removed})", flush=True)
        else:
            local.append(f"clear_all returned {removed}, expected 3")

        if len(um.jobs) == 0 and len(um.order) == 0:
            print(f"[PASS] state cleared (jobs=0, order=0)", flush=True)
        else:
            local.append(f"state not cleared: jobs={len(um.jobs)}, order={len(um.order)}")

        clear_events = [e for e in broadcasted if e.get("type") == "clear_all"]
        if clear_events and clear_events[0].get("removed") == 3:
            print(f"[PASS] broadcast clear_all event with removed=3", flush=True)
        else:
            local.append(f"missing clear_all broadcast: {broadcasted}")

        # Empty-case: clear_all() lần 2 → không broadcast (removed=0)
        broadcasted.clear()
        removed2 = await um.clear_all()
        if removed2 == 0 and not broadcasted:
            print(f"[PASS] clear_all empty-case: returned 0, no broadcast", flush=True)
        else:
            local.append(f"empty clear_all: removed={removed2}, broadcasts={broadcasted}")

        return local

    failures.extend(asyncio.run(_check_mgr()))

    # 3. Endpoint registered
    server_src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    if '/api/upi/jobs/clear-all' in server_src and 'clear_all_upi_jobs' in server_src:
        print(f"[PASS] server.py /api/upi/jobs/clear-all endpoint", flush=True)
    else:
        failures.append("server.py missing /api/upi/jobs/clear-all endpoint")

    if 'await um.clear_all()' in server_src:
        print(f"[PASS] server.py awaits um.clear_all()", flush=True)
    else:
        failures.append("server.py không await um.clear_all()")

    # 4. UI wiring
    html_src = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    if 'id="upi-btn-clear-all"' in html_src:
        print(f"[PASS] index.html upi-btn-clear-all button", flush=True)
    else:
        failures.append("index.html missing upi-btn-clear-all")

    js_src = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")
    for needle in (
        "btnClearAll:   $('upi-btn-clear-all')",
        "dom.btnClearAll.addEventListener('click'",
        "Dialog.confirm",
        "/api/upi/jobs/clear-all",
        "data.type === 'clear_all'",
    ):
        if needle in js_src:
            print(f"[PASS] upi.js contains: {needle}", flush=True)
        else:
            failures.append(f"upi.js missing: {needle}")
            print(f"[FAIL] upi.js missing: {needle}", flush=True)

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
