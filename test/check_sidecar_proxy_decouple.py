"""Phase 11.6 — verify sidecar proxy decoupling.

Goals:
  1. ``SentinelSidecar`` exposes ``proxy`` property → caller can detect
     whether IP-bound cookies are safe to sync.
  2. ``request_phase._run_request_phase_sync`` honours
     ``SIDECAR_SHARED_PROXY`` env (decouples sidecar proxy from request
     proxy → pool keys all signups to one Camoufox).
  3. ``_import_cookies_from_sidecar`` accepts ``caller_proxy`` kwarg and
     SKIPS Cloudflare IP-bound cookies when sidecar/caller proxies differ.
  4. Both call sites pass ``caller_proxy=request.proxy``.

Run: python3 test/check_sidecar_proxy_decouple.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK:   {msg}")


def main() -> int:
    sc = (ROOT / "sentinel_sidecar.py").read_text()
    rp = (ROOT / "request_phase.py").read_text()

    # 1. SentinelSidecar.proxy property
    sc_tree = ast.parse(sc)
    sidecar_cls: ast.ClassDef | None = None
    for node in ast.walk(sc_tree):
        if isinstance(node, ast.ClassDef) and node.name == "SentinelSidecar":
            sidecar_cls = node
            break
    if sidecar_cls is None:
        fail("SentinelSidecar class missing")
    proxy_prop = None
    for child in sidecar_cls.body:
        if (
            isinstance(child, ast.FunctionDef)
            and child.name == "proxy"
        ):
            # Must be decorated with @property
            for deco in child.decorator_list:
                if isinstance(deco, ast.Name) and deco.id == "property":
                    proxy_prop = child
                    break
    if proxy_prop is None:
        fail("SentinelSidecar.proxy @property missing")
    ok("SentinelSidecar.proxy @property present")

    # 2. SIDECAR_SHARED_PROXY env honoured in request_phase
    if "SIDECAR_SHARED_PROXY" not in rp:
        fail("request_phase doesn't read SIDECAR_SHARED_PROXY env")
    ok("request_phase reads SIDECAR_SHARED_PROXY env")
    # Look for the override branch
    if not re.search(
        r'shared_proxy\s*=\s*os\.getenv\("SIDECAR_SHARED_PROXY"\)', rp
    ):
        fail("SIDECAR_SHARED_PROXY not read via os.getenv pattern")
    ok("SIDECAR_SHARED_PROXY read via os.getenv")
    # Ensure the proxy passed to SentinelSidecar uses sidecar_proxy variable
    if "SentinelSidecar(\n                proxy=sidecar_proxy" not in rp:
        # Tolerate whitespace; check if proxy=sidecar_proxy appears in
        # context of SentinelSidecar(.
        m = re.search(
            r"SentinelSidecar\(\s*\n[^)]*proxy\s*=\s*sidecar_proxy",
            rp,
        )
        if not m:
            fail("SentinelSidecar() doesn't pass proxy=sidecar_proxy")
    ok("SentinelSidecar() launched with sidecar_proxy variable")

    # 3. _import_cookies_from_sidecar accepts caller_proxy + skips CF
    rp_tree = ast.parse(rp)
    import_fn: ast.FunctionDef | None = None
    for node in ast.walk(rp_tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_import_cookies_from_sidecar"
        ):
            import_fn = node
            break
    if import_fn is None:
        fail("_import_cookies_from_sidecar function missing")
    sig_args = [a.arg for a in import_fn.args.args]
    if "caller_proxy" not in sig_args:
        fail(f"_import_cookies_from_sidecar missing caller_proxy arg "
             f"(have: {sig_args})")
    ok("_import_cookies_from_sidecar has caller_proxy arg")

    if "_CF_IP_BOUND_COOKIES" not in rp:
        fail("_CF_IP_BOUND_COOKIES set missing")
    ok("_CF_IP_BOUND_COOKIES set defined")
    # Must contain the 4 Cloudflare cookies
    for cf in ("__cf_bm", "__cflb", "_cfuvid", "cf_clearance"):
        if f'"{cf}"' not in rp:
            fail(f"_CF_IP_BOUND_COOKIES missing {cf}")
    ok("_CF_IP_BOUND_COOKIES contains all 4 CF cookies")

    # 4. Both call sites pass caller_proxy=request.proxy
    call_count = len(re.findall(
        r"_import_cookies_from_sidecar\(\s*\n[^)]*"
        r"caller_proxy\s*=\s*request\.proxy",
        rp,
    ))
    bare_count = len(re.findall(
        r"_import_cookies_from_sidecar\(session, sidecar, log\)", rp
    ))
    if bare_count > 0:
        fail(
            f"{bare_count} _import_cookies_from_sidecar call(s) still "
            f"use 3-arg form (missing caller_proxy)"
        )
    if call_count < 2:
        fail(
            f"Only {call_count} _import_cookies_from_sidecar call(s) "
            f"pass caller_proxy=request.proxy; expected ≥2"
        )
    ok(
        f"{call_count} _import_cookies_from_sidecar call(s) pass "
        f"caller_proxy=request.proxy"
    )

    print("\nAll proxy-decouple checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
