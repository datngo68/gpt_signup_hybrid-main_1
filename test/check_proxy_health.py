"""Verify web/proxy_health.py — probe classify + acquire_live_proxy loop + knobs.

Fake probe inject → no-network, deterministic. Convention tNN→int.
Run: .venv/bin/python test/check_proxy_health.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import gpt_signup_hybrid_new.web.proxy_health as ph  # noqa: E402
from gpt_signup_hybrid_new.web.proxy_health import (  # noqa: E402
    _acquire_kwargs,
    _classify_exc,
    _load_proxy_knobs,
    acquire_live_proxy,
)


class FakePool:
    """Pool tối giản: pick round-robin live entries; track mark_dead key."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.dead: set[str] = set()
        self.cursor = 0
        self.pick_calls = 0
        self.mark_dead_calls: list[str] = []

    def is_active(self) -> bool:
        return any(e not in self.dead for e in self.entries)

    def pick(self):
        self.pick_calls += 1
        live = [e for e in self.entries if e not in self.dead]
        if not live:
            return None
        url = live[self.cursor % len(live)]
        self.cursor += 1
        return url

    def mark_dead(self, line):
        self.mark_dead_calls.append(line)
        self.dead.add(line)
        return True


def _make_probe(results):
    """results: list[(ok, reason)] consumed theo thứ tự gọi; track call count."""
    state = {"calls": 0}

    async def probe(url, **kwargs):
        i = state["calls"]
        state["calls"] += 1
        if i < len(results):
            return results[i]
        return results[-1] if results else (False, "ip")

    probe.state = state  # type: ignore[attr-defined]
    return probe


_KW = dict(endpoint="https://x", timeout=6, max_tries=5, sid_len=8,
           sid_retry_per_line=2, probe_concurrency=8)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def t01_empty_pool() -> int:
    pool = FakePool([])
    probe = _make_probe([(True, "ok")])
    url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    if (url, line) != (None, None) or probe.state["calls"] != 0:
        print(f"[FAIL] t01 empty :: {(url,line)} calls={probe.state['calls']}", flush=True)
        return 1
    print("[PASS] t01 empty pool → (None,None), no probe", flush=True)
    return 0


def t02_probe_ok() -> int:
    pool = FakePool(["h:1:u-{SID}:p"])
    probe = _make_probe([(True, "ok")])
    url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    if "{SID}" in (url or "") or "{" in (url or "") or line != "h:1:u-{SID}:p":
        print(f"[FAIL] t02 :: url={url} line={line}", flush=True)
        return 1
    if not re.match(r"^http://u-[a-z0-9]{8}:p@h:1$", url):
        print(f"[FAIL] t02 url not concrete :: {url}", flush=True)
        return 1
    print(f"[PASS] t02 probe ok → ({url}, raw)", flush=True)
    return 0


def t03_rotate_then_ok() -> int:
    pool = FakePool(["h:1:u-{SID}:p"])
    probe = _make_probe([(False, "ip"), (True, "ok")])
    url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    if url is None or line != "h:1:u-{SID}:p" or pool.mark_dead_calls:
        print(f"[FAIL] t03 :: url={url} dead={pool.mark_dead_calls}", flush=True)
        return 1
    if probe.state["calls"] != 2:
        print(f"[FAIL] t03 probe calls != 2 :: {probe.state['calls']}", flush=True)
        return 1
    print("[PASS] t03 ip → rotate SID → ok (no mark_dead)", flush=True)
    return 0


def t04_auth_mark_dead() -> int:
    pool = FakePool(["bad:1:u:p", "h:2:u-{SID}:p"])
    probe = _make_probe([(False, "auth"), (True, "ok")])
    url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    if pool.mark_dead_calls != ["bad:1:u:p"]:
        print(f"[FAIL] t04 mark_dead key :: {pool.mark_dead_calls}", flush=True)
        return 1
    if line != "h:2:u-{SID}:p":
        print(f"[FAIL] t04 next line :: {line}", flush=True)
        return 1
    print("[PASS] t04 auth → mark_dead(raw line) → next", flush=True)
    return 0


def t05_nonsid_ip_next() -> int:
    pool = FakePool(["h:1:u:p", "h:2:u2:p2"])
    probe = _make_probe([(False, "ip"), (True, "ok")])
    url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    # non-SID line ip-fail → KHÔNG rotate (1 probe), next line, mark_dead KHÔNG gọi
    if pool.mark_dead_calls or line != "h:2:u2:p2" or probe.state["calls"] != 2:
        print(f"[FAIL] t05 :: line={line} dead={pool.mark_dead_calls} calls={probe.state['calls']}", flush=True)
        return 1
    print("[PASS] t05 non-SID ip → no rotate, next line", flush=True)
    return 0


def t06_exhaust_max_tries() -> int:
    pool = FakePool(["h:1:u-{SID}:p"])
    probe = _make_probe([(False, "ip")])  # luôn ip
    kw = {**_KW, "max_tries": 5, "sid_retry_per_line": 2}
    url, line = _run(acquire_live_proxy(pool, probe=probe, **kw))
    if (url, line) != (None, None) or probe.state["calls"] != 5:
        print(f"[FAIL] t06 :: {(url,line)} calls={probe.state['calls']}", flush=True)
        return 1
    print("[PASS] t06 all ip → exhaust max_tries=5 → (None,None)", flush=True)
    return 0


def t07_sid_retry_cap() -> int:
    pool = FakePool(["h:1:u-{SID}:p"])
    probe = _make_probe([(False, "ip")])
    kw = {**_KW, "max_tries": 3, "sid_retry_per_line": 2}  # cap = min(3, 2+1)=3
    _run(acquire_live_proxy(pool, probe=probe, **kw))
    if probe.state["calls"] != 3:
        print(f"[FAIL] t07 sid cap :: calls={probe.state['calls']}", flush=True)
        return 1
    print("[PASS] t07 sid_retry cap = min(max_tries, sid_retry+1) = 3", flush=True)
    return 0


def t08_knob_loader() -> int:
    if _load_proxy_knobs({"proxy.max_tries": "9"})["max_tries"] != 9:
        print("[FAIL] t08 store override", flush=True)
        return 1
    if _load_proxy_knobs({})["max_tries"] != 5:
        print("[FAIL] t08 default", flush=True)
        return 1
    if _load_proxy_knobs({"proxy.max_tries": "99"})["max_tries"] != 5:
        print("[FAIL] t08 reject-range → default", flush=True)
        return 1
    k = _load_proxy_knobs({})
    if k["probe_concurrency"] != 4:
        print(f"[FAIL] t08 new knobs default :: {k}", flush=True)
        return 1
    print("[PASS] t08 knob loader (override/default/reject + 6 knob)", flush=True)
    return 0


def t09_classify() -> int:
    # exception-based classify
    if _classify_exc(Exception("Connection reset by peer")) != "ip":
        print("[FAIL] t09 reset→ip", flush=True)
        return 1
    if _classify_exc(Exception("Could not resolve host: foo")) != "auth":
        print("[FAIL] t09 resolve→auth", flush=True)
        return 1
    if _classify_exc(Exception("Proxy authentication required")) != "auth":
        print("[FAIL] t09 proxyauth→auth", flush=True)
        return 1

    # status-based via probe_proxy (mock AsyncSession — no network)
    import curl_cffi.requests as _cr

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    def _mk_session(code=None, exc=None):
        class _S:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                if exc:
                    raise exc
                return _Resp(code)

        return _S

    orig = _cr.AsyncSession
    try:
        _cr.AsyncSession = _mk_session(code=407)
        ok, reason = _run(ph.probe_proxy("http://u:p@h:1", endpoint="x", timeout=1))
        if ok or reason != "auth":
            print(f"[FAIL] t09 status 407 → {reason}", flush=True)
            return 1
        _cr.AsyncSession = _mk_session(code=503)
        ok, reason = _run(ph.probe_proxy("http://u:p@h:1", endpoint="x", timeout=1))
        if ok or reason != "ip":
            print(f"[FAIL] t09 status 503 → {reason}", flush=True)
            return 1
        _cr.AsyncSession = _mk_session(code=200)
        ok, reason = _run(ph.probe_proxy("http://u:p@h:1", endpoint="x", timeout=1))
        if not ok or reason != "ok":
            print(f"[FAIL] t09 status 200 → {reason}", flush=True)
            return 1
    finally:
        _cr.AsyncSession = orig
    print("[PASS] t09 classify (407→auth, 503→ip, 200→ok, exc reset/resolve)", flush=True)
    return 0


def t10_log_mask() -> int:
    logs: list[str] = []
    pool = FakePool(["h:1:user:realpass"])  # non-template, ip-fail → next → none
    probe = _make_probe([(False, "ip")])
    _run(acquire_live_proxy(pool, probe=probe, log=logs.append, **_KW))
    joined = "\n".join(logs)
    if "realpass" in joined:
        print(f"[FAIL] t10 leak :: {joined}", flush=True)
        return 1
    if not logs:
        print("[FAIL] t10 no log emitted", flush=True)
        return 1
    print("[PASS] t10 log mask (no plaintext pass)", flush=True)
    return 0


def t11_url_form_line() -> int:
    pool = FakePool(["http://u:p@h:1"])
    probe = _make_probe([(True, "ok")])
    url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    if url != "http://u:p@h:1" or line != "http://u:p@h:1":
        print(f"[FAIL] t11 :: url={url} line={line}", flush=True)
        return 1
    print("[PASS] t11 URL-form line probe ok → return as-is", flush=True)
    return 0


def t12_garbage_no_dos() -> int:
    pool = FakePool(["garbage", "h:1:u:p"])
    probe = _make_probe([(True, "ok")])
    try:
        url, line = _run(acquire_live_proxy(pool, probe=probe, **_KW))
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] t12 raised :: {type(exc).__name__}: {exc}", flush=True)
        return 1
    if "garbage" not in pool.mark_dead_calls or line != "h:1:u:p":
        print(f"[FAIL] t12 :: dead={pool.mark_dead_calls} line={line}", flush=True)
        return 1
    print("[PASS] t12 garbage → mark_dead + continue (no DoS)", flush=True)
    return 0


def t13_semaphore_bound() -> int:
    ph._ACQUIRE_SEM = None  # reset process-global để init với N=2
    in_flight = {"cur": 0, "max": 0}

    async def slow_probe(url, **kwargs):
        in_flight["cur"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["cur"])
        await asyncio.sleep(0.02)
        in_flight["cur"] -= 1
        return (True, "ok")

    async def run_all():
        kw = {**_KW, "probe_concurrency": 2}
        tasks = [
            acquire_live_proxy(FakePool(["h:1:u-{SID}:p"]), probe=slow_probe, **kw)
            for _ in range(5)
        ]
        return await asyncio.gather(*tasks)

    _run(run_all())
    ph._ACQUIRE_SEM = None  # cleanup
    if in_flight["max"] > 2 or in_flight["max"] < 2:
        print(f"[FAIL] t13 semaphore bound :: max in-flight={in_flight['max']} (expect 2)", flush=True)
        return 1
    print("[PASS] t13 semaphore bound max in-flight == 2 (interleave, ≤ N)", flush=True)
    return 0


def t14_run_with_proxy_rotation_materialize() -> int:
    import gpt_signup_hybrid_new.web.manager as mgr

    captured: list[str | None] = []

    async def func(proxy):
        captured.append(proxy)
        raise Exception("curl: (7) couldn't connect")  # network → mark_dead

    pool = FakePool(["h:1:u-{SID}:p"])
    pool.live_entries = lambda: [e for e in pool.entries if e not in pool.dead]  # type: ignore
    orig = mgr.get_proxy_pool
    try:
        mgr.get_proxy_pool = lambda: pool
        try:
            _run(mgr.run_with_proxy_rotation(func))
        except Exception:  # noqa: BLE001
            pass  # last_exc re-raised expected
    finally:
        mgr.get_proxy_pool = orig

    if not captured or "{SID}" in (captured[0] or ""):
        print(f"[FAIL] t14 func proxy not concrete :: {captured}", flush=True)
        return 1
    if not re.match(r"^http://u-[a-z0-9]{8}:p@h:1$", captured[0]):
        print(f"[FAIL] t14 not materialized URL :: {captured[0]}", flush=True)
        return 1
    if pool.mark_dead_calls != ["h:1:u-{SID}:p"]:
        print(f"[FAIL] t14 mark_dead raw line :: {pool.mark_dead_calls}", flush=True)
        return 1
    print("[PASS] t14 run_with_proxy_rotation materialize + mark_dead raw line", flush=True)
    return 0


def t15_acquire_kwargs_shape() -> int:
    knobs = _load_proxy_knobs({})
    kw = _acquire_kwargs(knobs)
    if set(kw) != {"endpoint", "timeout", "max_tries", "sid_len",
                   "sid_retry_per_line", "probe_concurrency"}:
        print(f"[FAIL] t15 acquire kwargs shape :: {set(kw)}", flush=True)
        return 1
    print("[PASS] t15 _acquire_kwargs shape match acquire_live_proxy signature", flush=True)
    return 0


def main() -> int:
    print("=== check_proxy_health ===", flush=True)
    tests = [
        t01_empty_pool, t02_probe_ok, t03_rotate_then_ok, t04_auth_mark_dead,
        t05_nonsid_ip_next, t06_exhaust_max_tries, t07_sid_retry_cap, t08_knob_loader,
        t09_classify, t10_log_mask, t11_url_form_line, t12_garbage_no_dos,
        t13_semaphore_bound, t14_run_with_proxy_rotation_materialize,
        t15_acquire_kwargs_shape,
    ]
    failures = 0
    for fn in tests:
        try:
            rc = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} :: raised {type(exc).__name__}: {exc}", flush=True)
            rc = 1
        if rc != 0:
            failures += 1
    print(f"=== done :: {len(tests) - failures}/{len(tests)} pass ===", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
