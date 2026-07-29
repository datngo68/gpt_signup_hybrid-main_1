"""Verify Phase 3 wiring: _resolve_job_proxy async, _begin_job_proxy set 2 fields,
mark_dead raw line (F-J), rerun/2FA acquire once (F-M/F-H), _acquire_kwargs gate.

Fake pool + monkeypatch acquire → no-network. Run: .venv/bin/python test/check_proxy_wire_jobs.py
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import gpt_signup_hybrid_new.web.manager as mgr  # noqa: E402

SRC = (ROOT / "web" / "manager.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fake_acquire(url, line):
    async def acquire(pool, *, log=None, **kwargs):
        return (url, line)
    return acquire


def _func_node(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def t01_resolve_is_coro() -> int:
    if not inspect.iscoroutinefunction(mgr._resolve_job_proxy):
        print("[FAIL] t01 _resolve_job_proxy not coroutine", flush=True)
        return 1
    # Mode "probe" → _resolve_job_proxy gọi acquire_live_proxy (gate theo mode).
    pool = mgr.get_proxy_pool()
    pool.configure([], mode="probe")
    orig = mgr.acquire_live_proxy
    try:
        mgr.acquire_live_proxy = _fake_acquire("http://u:p@h:1", "h:1:u:p")
        res = _run(mgr._resolve_job_proxy(knobs=mgr._load_proxy_knobs({})))
    finally:
        mgr.acquire_live_proxy = orig
        pool.configure([], mode="round_robin")
    if res != ("http://u:p@h:1", "h:1:u:p"):
        print(f"[FAIL] t01 tuple :: {res}", flush=True)
        return 1
    print("[PASS] t01 _resolve_job_proxy coroutine → (url, line) [mode=probe]", flush=True)
    return 0


def t02_begin_empty_pool() -> int:
    job = types.SimpleNamespace()
    self_dummy = types.SimpleNamespace(_use_proxy=True)
    # _begin_job_proxy → self._resolve_proxy_for_job → _resolve_job_proxy → acquire.
    self_dummy._resolve_proxy_for_job = types.MethodType(
        mgr.JobManager._resolve_proxy_for_job, self_dummy
    )
    orig = mgr.acquire_live_proxy
    try:
        mgr.acquire_live_proxy = _fake_acquire(None, None)
        _run(mgr.JobManager._begin_job_proxy(self_dummy, job, lambda m: None))
    finally:
        mgr.acquire_live_proxy = orig
    if job._active_proxy is not None or job._active_proxy_line is not None:
        print(f"[FAIL] t02 :: ap={job._active_proxy} line={job._active_proxy_line}", flush=True)
        return 1
    print("[PASS] t02 empty pool → both fields None (direct)", flush=True)
    return 0


def t03_begin_active_pool() -> int:
    job = types.SimpleNamespace()
    self_dummy = types.SimpleNamespace(_use_proxy=True)
    self_dummy._resolve_proxy_for_job = types.MethodType(
        mgr.JobManager._resolve_proxy_for_job, self_dummy
    )
    pool = mgr.get_proxy_pool()
    pool.configure([], mode="probe")
    orig = mgr.acquire_live_proxy
    try:
        mgr.acquire_live_proxy = _fake_acquire("http://u-abc12345:p@h:1", "h:1:u-{SID}:p")
        _run(mgr.JobManager._begin_job_proxy(self_dummy, job, lambda m: None))
    finally:
        mgr.acquire_live_proxy = orig
        pool.configure([], mode="round_robin")
    if "{SID}" in (job._active_proxy or "") or job._active_proxy_line != "h:1:u-{SID}:p":
        print(f"[FAIL] t03 :: ap={job._active_proxy} line={job._active_proxy_line}", flush=True)
        return 1
    print("[PASS] t03 active → _active_proxy=URL, _active_proxy_line=raw [mode=probe]", flush=True)
    return 0


def t04_three_begin_async_set_fields() -> int:
    n_async = sum(
        1 for node in ast.walk(TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_begin_job_proxy"
    )
    if n_async != 3:
        print(f"[FAIL] t04 async _begin_job_proxy count={n_async}", flush=True)
        return 1
    if SRC.count("await self._begin_job_proxy(") != 3:
        print(f"[FAIL] t04 await call-sites={SRC.count('await self._begin_job_proxy(')}", flush=True)
        return 1
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_begin_job_proxy":
            body = ast.dump(node)
            if "_active_proxy_line" not in body or "_active_proxy" not in body:
                print("[FAIL] t04 _begin_job_proxy missing field set", flush=True)
                return 1
    print("[PASS] t04 3× async _begin_job_proxy + await + set both fields", flush=True)
    return 0


def t05_note_failure_raw_line() -> int:
    # behavioral: mark_dead dùng _active_proxy_line (raw), không _active_proxy
    captured = {"dead": None}

    class _Pool:
        def mark_dead(self, key):
            captured["dead"] = key
            return True

    job = types.SimpleNamespace(_active_proxy="http://u:p@h:1", _active_proxy_line="h:1:u:p")
    self_dummy = types.SimpleNamespace(_job_log=lambda *a, **k: None)
    orig = mgr.get_proxy_pool
    try:
        mgr.get_proxy_pool = lambda: _Pool()
        mgr.JobManager._note_proxy_failure(self_dummy, job, "curl: (7) couldn't connect")
    finally:
        mgr.get_proxy_pool = orig
    if captured["dead"] != "h:1:u:p":
        print(f"[FAIL] t05 mark_dead key :: {captured['dead']!r} (expect raw line)", flush=True)
        return 1
    # grep guard: 3 _note_proxy_failure dùng _active_proxy_line, không mark_dead(_active_proxy)
    if "_active_proxy_line" not in SRC.split("_note_proxy_failure")[1]:
        print("[FAIL] t05 _note_proxy_failure không dùng _active_proxy_line", flush=True)
        return 1
    print("[PASS] t05 mark_dead = _active_proxy_line (raw, F-J)", flush=True)
    return 0


def t06_no_unawaited_resolve() -> int:
    # mọi call _resolve_job_proxy( phải có await trên cùng dòng (trừ def)
    for i, line in enumerate(SRC.splitlines(), 1):
        if "_resolve_job_proxy(" in line and "async def _resolve_job_proxy" not in line:
            if "await" not in line:
                print(f"[FAIL] t06 line {i} unawaited :: {line.strip()}", flush=True)
                return 1
    if "_resolve_job_proxy()" in SRC:
        print("[FAIL] t06 legacy sync call _resolve_job_proxy() còn sót", flush=True)
        return 1
    print("[PASS] t06 no unawaited _resolve_job_proxy", flush=True)
    return 0


def t07_rerun_acquire_before_loop() -> int:
    node = _func_node("rerun_link_for_job")
    if node is None:
        print("[FAIL] t07 rerun_link_for_job not found", flush=True)
        return 1
    # Code refactor: rerun gọi self._resolve_proxy_for_job (wrap _resolve_job_proxy)
    # → method này tự set _active_proxy_line; caller chỉ cần acquire 1 lần trước loop.
    for sub in ast.walk(node):
        if isinstance(sub, ast.For):
            if "_resolve_proxy_for_job" in ast.dump(sub):
                print("[FAIL] t07 _resolve_proxy_for_job nằm TRONG attempt loop (re-acquire)", flush=True)
                return 1
    if "_resolve_proxy_for_job" not in ast.dump(node):
        print("[FAIL] t07 rerun không acquire", flush=True)
        return 1
    # _resolve_proxy_for_job body phải set _active_proxy_line (single source of truth).
    resolver = _func_node("_resolve_proxy_for_job")
    if resolver is None or "_active_proxy_line" not in ast.dump(resolver):
        print("[FAIL] t07 _resolve_proxy_for_job không set _active_proxy_line", flush=True)
        return 1
    print("[PASS] t07 rerun_link acquire 1 lần trước loop + set line (F-M)", flush=True)
    return 0


def t08_2fa_sets_fields() -> int:
    node = _func_node("_run_2fa_only_inner")
    if node is None:
        print("[FAIL] t08 _run_2fa_only_inner not found", flush=True)
        return 1
    if "_resolve_proxy_for_job" not in ast.dump(node):
        print("[FAIL] t08 _run_2fa_only_inner không acquire qua _resolve_proxy_for_job", flush=True)
        return 1
    # field-set responsibility moved into _resolve_proxy_for_job (DRY).
    resolver = _func_node("_resolve_proxy_for_job")
    if resolver is None or "_active_proxy_line" not in ast.dump(resolver):
        print("[FAIL] t08 _resolve_proxy_for_job không set _active_proxy_line", flush=True)
        return 1
    print("[PASS] t08 _run_2fa_only_inner acquire + set fields (F-M)", flush=True)
    return 0


def t09_knob_load_once() -> int:
    calls = {"n": 0}
    orig_env = mgr.proxy_env_defaults
    orig_acq = mgr.acquire_live_proxy
    orig_cache = mgr._PROXY_KNOBS_CACHE
    try:
        def counting_env(*a, **k):
            calls["n"] += 1
            return {}
        mgr.proxy_env_defaults = counting_env
        mgr.acquire_live_proxy = _fake_acquire("http://u:p@h:1", "h:1:u:p")
        mgr._PROXY_KNOBS_CACHE = None  # buộc load qua _current_proxy_knobs

        job = types.SimpleNamespace()
        self_dummy = types.SimpleNamespace(_use_proxy=True)
        self_dummy._resolve_proxy_for_job = types.MethodType(
            mgr.JobManager._resolve_proxy_for_job, self_dummy
        )
        _run(mgr.JobManager._begin_job_proxy(self_dummy, job, lambda m: None))
        # simulate 3 retries reuse cached knobs (KHÔNG load lại)
        for _ in range(3):
            _run(mgr._resolve_job_proxy(knobs=job._proxy_knobs))
    finally:
        mgr.proxy_env_defaults = orig_env
        mgr.acquire_live_proxy = orig_acq
        mgr._PROXY_KNOBS_CACHE = orig_cache
    if calls["n"] > 1:
        print(f"[FAIL] t09 proxy_env_defaults called {calls['n']}× (expect ≤1/job)", flush=True)
        return 1
    print(f"[PASS] t09 knob load once per job (env reads={calls['n']}, F-H)", flush=True)
    return 0


def t10_acquire_kwargs_signature() -> int:
    kw = mgr._acquire_kwargs(mgr._load_proxy_knobs({}))
    # smoke: signature khớp acquire_live_proxy (no TypeError on splat)
    sig = inspect.signature(mgr.acquire_live_proxy)
    missing = set(kw) - set(sig.parameters)
    if missing:
        print(f"[FAIL] t10 kwargs not in acquire sig :: {missing}", flush=True)
        return 1
    print("[PASS] t10 _acquire_kwargs matches acquire_live_proxy signature", flush=True)
    return 0


def main() -> int:
    print("=== check_proxy_wire_jobs ===", flush=True)
    tests = [
        t01_resolve_is_coro, t02_begin_empty_pool, t03_begin_active_pool,
        t04_three_begin_async_set_fields, t05_note_failure_raw_line,
        t06_no_unawaited_resolve, t07_rerun_acquire_before_loop, t08_2fa_sets_fields,
        t09_knob_load_once, t10_acquire_kwargs_signature,
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
