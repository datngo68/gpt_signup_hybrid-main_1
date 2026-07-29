"""Verify Option A + Fix A/B: autoreg dùng pool.acquire/release (least-used +
no-immediate-repeat) thay pool.pick → N email song song với ≥N proxy live = mỗi
email 1 IP riêng. Đồng thời gate ``reg.use_proxy`` (Fix A: setting bypass) +
log proxy mỗi email (Fix B: observability).

Test plan:
  T01 syntax_ok — parse autoreg/runner.py + web/manager.py AST.
  T02 autoreg_callsites — _process_email gọi _acquire_job_proxy_lease (await + unpack 3),
      KHÔNG còn gọi _resolve_job_proxy ở đó. Có finally release.
  T03 manager_helpers_defined — _acquire_job_proxy_lease + _release_job_proxy_lease
      defined với signature đúng (async + 3-tuple return).
  T04 acquire_parallel_distinct — 3 acquire song song với pool 3 proxy → 3 line khác
      nhau (least-used + no-repeat đảm bảo no-conflict khi worker đồng thời).
  T05 acquire_release_round_trip — acquire → release → re-acquire cùng line OK
      (lease giảm đúng, no leak).
  T06 bad_format_retry_then_dead — mock pool trả line format rác →
      _acquire_job_proxy_lease release + mark_dead + retry tối đa 3 lần;
      pool toàn rác → (None, None, False).
  T07 probe_mode_no_lease — pool.mode='probe' → fallback acquire_live_proxy,
      requires_release=False (release helper no-op).
  T08 release_helper_safe — requires_release=False / line=None → no-op (idempotent).
  T09 use_proxy_gate — _process_email gate ``self._config.use_proxy`` TRƯỚC khi
      gọi _acquire_job_proxy_lease (Fix A — autoreg respect setting).
  T10 config_field_use_proxy — AutoRegConfig có field ``use_proxy: bool``
      default True.
  T11 routes_inject_use_proxy — icloud_routes.autoreg_start truyền kwarg
      ``use_proxy=`` lấy từ Settings ``reg.use_proxy``.
  T12 log_proxy_per_email — _process_email log ``[proxy] {mask}`` sau acquire
      (Fix B — observability).

Run: .venv/bin/python test/check_autoreg_proxy_lease.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "autoreg" / "runner.py"
MANAGER = ROOT / "web" / "manager.py"
RUNNER_SRC = RUNNER.read_text(encoding="utf-8")
MANAGER_SRC = MANAGER.read_text(encoding="utf-8")


def t01_syntax_ok() -> int:
    try:
        globals()["_RUNNER_TREE"] = ast.parse(RUNNER_SRC)
        globals()["_MANAGER_TREE"] = ast.parse(MANAGER_SRC)
    except SyntaxError as exc:
        print(f"[FAIL] t01 syntax :: {exc}", flush=True)
        return 1
    print("[PASS] t01 runner.py + manager.py parse AST OK", flush=True)
    return 0


def _find_process_email(tree: ast.AST) -> ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_email":
            return node
    return None


def t02_autoreg_callsites() -> int:
    tree = globals()["_RUNNER_TREE"]
    fn = _find_process_email(tree)
    if fn is None:
        print("[FAIL] t02 không tìm thấy _process_email", flush=True)
        return 1
    src_fn = ast.unparse(fn)

    # Có call _acquire_job_proxy_lease (await + unpack 3 phần tử)
    acquire_unpack_ok = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if not (isinstance(val, ast.Await)
                and isinstance(val.value, ast.Call)
                and isinstance(val.value.func, ast.Name)
                and val.value.func.id == "_acquire_job_proxy_lease"):
            continue
        tgt = node.targets[0]
        if isinstance(tgt, ast.Tuple) and len(tgt.elts) == 3:
            acquire_unpack_ok = True
            break
    if not acquire_unpack_ok:
        print("[FAIL] t02 không thấy await _acquire_job_proxy_lease() unpack 3-tuple", flush=True)
        return 1

    # KHÔNG còn gọi _resolve_job_proxy trong _process_email (đã thay)
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_resolve_job_proxy"):
            print("[FAIL] t02 _process_email vẫn gọi _resolve_job_proxy (path cũ chưa thay)", flush=True)
            return 1

    # Có finally block chứa call _release_job_proxy_lease
    has_release_in_finally = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        if not node.finalbody:
            continue
        final_src = ast.unparse(ast.Module(body=node.finalbody, type_ignores=[]))
        if "_release_job_proxy_lease(" in final_src:
            has_release_in_finally = True
            break
    if not has_release_in_finally:
        print("[FAIL] t02 không thấy _release_job_proxy_lease() trong finally", flush=True)
        return 1

    # _proxy_leased khai báo trước try (default False) — safe khi acquire raise
    if "_proxy_leased: bool = False" not in src_fn:
        print("[FAIL] t02 không init `_proxy_leased: bool = False` trước try", flush=True)
        return 1

    print("[PASS] t02 _process_email: acquire(3-tuple) + finally release + _proxy_leased init", flush=True)
    return 0


def t03_manager_helpers_defined() -> int:
    tree = globals()["_MANAGER_TREE"]
    acquire_fn: ast.AsyncFunctionDef | None = None
    release_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_acquire_job_proxy_lease":
            acquire_fn = node
        elif isinstance(node, ast.FunctionDef) and node.name == "_release_job_proxy_lease":
            release_fn = node
    if acquire_fn is None:
        print("[FAIL] t03 không thấy async def _acquire_job_proxy_lease trong web/manager.py", flush=True)
        return 1
    if release_fn is None:
        print("[FAIL] t03 không thấy def _release_job_proxy_lease trong web/manager.py", flush=True)
        return 1

    # acquire: phải gọi pool.acquire (path round_robin/random) + acquire_live_proxy (probe path)
    acquire_src = ast.unparse(acquire_fn)
    if "pool.acquire(" not in acquire_src:
        print("[FAIL] t03 _acquire_job_proxy_lease không gọi pool.acquire()", flush=True)
        return 1
    if "acquire_live_proxy(" not in acquire_src:
        print("[FAIL] t03 _acquire_job_proxy_lease không gọi acquire_live_proxy (probe path)", flush=True)
        return 1
    if "materialize_proxy(" not in acquire_src:
        print("[FAIL] t03 _acquire_job_proxy_lease không materialize line", flush=True)
        return 1

    # release: phải gọi pool.release; gate bằng requires_release
    release_src = ast.unparse(release_fn)
    if "pool.release(" not in release_src and ".release(" not in release_src:
        print("[FAIL] t03 _release_job_proxy_lease không gọi pool.release()", flush=True)
        return 1
    if "requires_release" not in release_src:
        print("[FAIL] t03 _release_job_proxy_lease không gate bằng requires_release flag", flush=True)
        return 1

    print("[PASS] t03 helper _acquire_job_proxy_lease + _release_job_proxy_lease defined đúng API", flush=True)
    return 0


# ── Runtime tests (cần pool + manager helper thật) ──────────────────────


def _import_pool_and_helpers():
    sys.path.insert(0, str(ROOT))
    from web.proxy_pool import ProxyPool, get_proxy_pool
    from web import manager as mgr
    return ProxyPool, get_proxy_pool, mgr


def t04_acquire_parallel_distinct() -> int:
    """3 acquire song song với pool 3 proxy → 3 line distinct (least-used)."""
    ProxyPool, get_proxy_pool, mgr = _import_pool_and_helpers()

    pool = get_proxy_pool()
    pool.configure(["h1:1:u:p", "h2:2:u:p", "h3:3:u:p"], mode="random")

    async def _exercise():
        results = await asyncio.gather(
            mgr._acquire_job_proxy_lease(),
            mgr._acquire_job_proxy_lease(),
            mgr._acquire_job_proxy_lease(),
        )
        # release tất cả để không leak
        for _url, line, leased in results:
            mgr._release_job_proxy_lease(line, leased)
        return results

    results = asyncio.run(_exercise())
    lines = [r[1] for r in results]
    leased = [r[2] for r in results]

    if not all(leased):
        print(f"[FAIL] t04 expect tất cả leased=True (mode random) :: {leased}", flush=True)
        return 1
    if len(set(lines)) != 3:
        print(f"[FAIL] t04 3 acquire song song nhưng có TRÙNG :: {lines}", flush=True)
        return 1
    print(f"[PASS] t04 3 acquire parallel = 3 line distinct :: {lines}", flush=True)
    return 0


def t05_acquire_release_round_trip() -> int:
    """acquire → release → state sạch (lease=0); re-acquire pool 1 entry OK."""
    ProxyPool, get_proxy_pool, mgr = _import_pool_and_helpers()
    pool = get_proxy_pool()
    pool.configure(["only:1:u:p"], mode="round_robin")

    async def _exercise():
        url1, line1, leased1 = await mgr._acquire_job_proxy_lease()
        mgr._release_job_proxy_lease(line1, leased1)
        url2, line2, leased2 = await mgr._acquire_job_proxy_lease()
        mgr._release_job_proxy_lease(line2, leased2)
        return (url1, line1, leased1), (url2, line2, leased2)

    (u1, l1, b1), (u2, l2, b2) = asyncio.run(_exercise())
    if not (b1 and b2):
        print("[FAIL] t05 expect leased=True cho cả 2 lần acquire", flush=True)
        return 1
    if l1 != l2:
        print(f"[FAIL] t05 expect cùng line (pool 1 entry) :: {l1} vs {l2}", flush=True)
        return 1
    # State pool sạch sau release
    if pool._leases:  # noqa: SLF001 — test inspect internal
        print(f"[FAIL] t05 sau 2 release, _leases phải rỗng :: {pool._leases}", flush=True)
        return 1
    print(f"[PASS] t05 acquire→release round-trip OK, leases dict empty", flush=True)
    return 0


def t06_bad_format_retry_then_dead() -> int:
    """Pool toàn line format rác → mark_dead toàn bộ + return (None, None, False)."""
    ProxyPool, get_proxy_pool, mgr = _import_pool_and_helpers()
    pool = get_proxy_pool()
    pool.configure(["@", "@@", "###"], mode="random")  # toàn format không parse được

    async def _exercise():
        return await mgr._acquire_job_proxy_lease()

    url, line, leased = asyncio.run(_exercise())
    if (url, line, leased) != (None, None, False):
        print(f"[FAIL] t06 expect (None, None, False) :: ({url}, {line}, {leased})", flush=True)
        return 1
    # Tất cả entries đã bị mark_dead
    if pool.is_active():
        print(f"[FAIL] t06 expect pool inactive (toàn dead) :: dead={pool._dead}", flush=True)  # noqa: SLF001
        return 1
    print("[PASS] t06 format rác → mark_dead + (None, None, False)", flush=True)
    return 0


def t07_probe_mode_no_lease() -> int:
    """Probe mode → fallback acquire_live_proxy, leased=False (no release needed)."""
    ProxyPool, get_proxy_pool, mgr = _import_pool_and_helpers()
    pool = get_proxy_pool()
    pool.configure(["h:1:u:p"], mode="probe")

    captured = {"called": False}

    async def fake_acquire_live(pool_arg, *, log=None, **kw):
        captured["called"] = True
        return ("http://u:p@h:1", "h:1:u:p")

    orig = mgr.acquire_live_proxy
    mgr.acquire_live_proxy = fake_acquire_live  # type: ignore[assignment]
    try:
        async def _exercise():
            return await mgr._acquire_job_proxy_lease()
        url, line, leased = asyncio.run(_exercise())
    finally:
        mgr.acquire_live_proxy = orig
        pool.configure(None, mode="round_robin")  # reset mode

    if not captured["called"]:
        print("[FAIL] t07 probe mode không fallback acquire_live_proxy", flush=True)
        return 1
    if leased is not False:
        print(f"[FAIL] t07 probe mode expect leased=False (no lease) :: {leased}", flush=True)
        return 1
    if (url, line) != ("http://u:p@h:1", "h:1:u:p"):
        print(f"[FAIL] t07 probe mode trả sai tuple :: ({url}, {line})", flush=True)
        return 1
    print("[PASS] t07 probe mode → acquire_live_proxy + leased=False", flush=True)
    return 0


def t08_release_helper_safe() -> int:
    """release helper idempotent: requires_release=False / line=None → no-op."""
    ProxyPool, get_proxy_pool, mgr = _import_pool_and_helpers()
    pool = get_proxy_pool()
    pool.configure(["x:1:u:p"], mode="random")

    # Setup: acquire 1 lần để lease=1 cho 'x:1:u:p'
    async def _setup():
        return await mgr._acquire_job_proxy_lease()
    _url, line, leased = asyncio.run(_setup())

    # release với requires_release=False → KHÔNG giảm lease
    mgr._release_job_proxy_lease(line, False)
    if pool._leases.get(line, 0) != 1:  # noqa: SLF001
        print(f"[FAIL] t08 requires_release=False phải no-op, leases={pool._leases}", flush=True)  # noqa: SLF001
        return 1

    # release với line=None → KHÔNG raise
    mgr._release_job_proxy_lease(None, True)

    # release đúng cách → lease về 0
    mgr._release_job_proxy_lease(line, leased)
    if pool._leases:  # noqa: SLF001
        print(f"[FAIL] t08 release đúng phải clear lease, _leases={pool._leases}", flush=True)  # noqa: SLF001
        return 1
    print("[PASS] t08 release helper idempotent với False / None", flush=True)
    return 0


def t09_use_proxy_gate() -> int:
    """_process_email gate self._config.use_proxy TRƯỚC khi gọi acquire."""
    tree = globals()["_RUNNER_TREE"]
    fn = _find_process_email(tree)
    if fn is None:
        print("[FAIL] t09 không tìm thấy _process_email", flush=True)
        return 1
    # Tìm If(test=Attribute(value=Attribute(self,_config),attr=use_proxy))
    # bao quanh call _acquire_job_proxy_lease.
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        # test phải reference self._config.use_proxy
        test_src = ast.unparse(node.test)
        if "self._config.use_proxy" not in test_src:
            continue
        body_src = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
        if "_acquire_job_proxy_lease(" in body_src:
            print("[PASS] t09 use_proxy gate bao quanh _acquire_job_proxy_lease", flush=True)
            return 0
    print("[FAIL] t09 không thấy `if self._config.use_proxy:` bao quanh acquire", flush=True)
    return 1


def t10_config_field_use_proxy() -> int:
    """AutoRegConfig có field use_proxy: bool default True."""
    sys.path.insert(0, str(ROOT))
    from autoreg.runner import AutoRegConfig

    cfg = AutoRegConfig()
    if not hasattr(cfg, "use_proxy"):
        print("[FAIL] t10 AutoRegConfig thiếu field use_proxy", flush=True)
        return 1
    if cfg.use_proxy is not True:
        print(f"[FAIL] t10 default use_proxy phải True (backward compat), got {cfg.use_proxy!r}", flush=True)
        return 1
    # Override OK
    cfg2 = AutoRegConfig(use_proxy=False)
    if cfg2.use_proxy is not False:
        print(f"[FAIL] t10 override use_proxy=False không hiệu lực :: {cfg2.use_proxy!r}", flush=True)
        return 1
    print("[PASS] t10 AutoRegConfig.use_proxy: bool default True, override OK", flush=True)
    return 0


def t11_routes_inject_use_proxy() -> int:
    """icloud_routes.autoreg_start truyền kwarg use_proxy lấy từ reg.use_proxy."""
    routes = ROOT / "web" / "icloud_routes.py"
    try:
        routes_tree = ast.parse(routes.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"[FAIL] t11 icloud_routes.py syntax :: {exc}", flush=True)
        return 1

    # Tìm AutoRegConfig(...) call có kwarg use_proxy
    for node in ast.walk(routes_tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AutoRegConfig"):
            continue
        kwargs = {k.arg: ast.unparse(k.value) for k in node.keywords if k.arg}
        if "use_proxy" not in kwargs:
            print("[FAIL] t11 AutoRegConfig(...) thiếu kwarg use_proxy", flush=True)
            return 1
        if "reg.use_proxy" not in kwargs["use_proxy"]:
            print(f"[FAIL] t11 use_proxy không đọc từ reg.use_proxy :: {kwargs['use_proxy']!r}", flush=True)
            return 1
        print(f"[PASS] t11 AutoRegConfig(use_proxy=...) lấy từ reg.use_proxy", flush=True)
        return 0
    print("[FAIL] t11 không thấy call AutoRegConfig(...) trong icloud_routes.py", flush=True)
    return 1


def t12_log_proxy_per_email() -> int:
    """_process_email log [proxy] {mask} sau acquire (observability Fix B)."""
    tree = globals()["_RUNNER_TREE"]
    fn = _find_process_email(tree)
    if fn is None:
        print("[FAIL] t12 không tìm thấy _process_email", flush=True)
        return 1
    src_fn = ast.unparse(fn)
    if "[proxy]" not in src_fn:
        print("[FAIL] t12 _process_email không log '[proxy]' sau acquire", flush=True)
        return 1
    if "mask_proxy" not in src_fn:
        print("[FAIL] t12 _process_email không import mask_proxy để mask credential", flush=True)
        return 1
    print("[PASS] t12 _process_email log [proxy] mask sau acquire", flush=True)
    return 0


def main() -> int:
    print("=== check_autoreg_proxy_lease ===", flush=True)
    rc = 0
    for fn in (
        t01_syntax_ok,
        t02_autoreg_callsites,
        t03_manager_helpers_defined,
        t04_acquire_parallel_distinct,
        t05_acquire_release_round_trip,
        t06_bad_format_retry_then_dead,
        t07_probe_mode_no_lease,
        t08_release_helper_safe,
        t09_use_proxy_gate,
        t10_config_field_use_proxy,
        t11_routes_inject_use_proxy,
        t12_log_proxy_per_email,
    ):
        try:
            rc |= fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} raised :: {type(exc).__name__}: {exc}", flush=True)
            rc |= 1
    print("=== DONE ===" if rc == 0 else "=== FAILED ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
