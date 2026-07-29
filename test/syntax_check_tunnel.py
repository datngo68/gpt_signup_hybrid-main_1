"""Syntax + import smoke check cho Cloudflare Tunnel feature.

Verify:
  1. AST parse các file đã sửa.
  2. db/repositories.py: key 'tunnel.cloudflare.enabled' có trong _EXACT_KEYS,
     validator chấp nhận bool, reject str/int.
  3. web/cloudflare_tunnel.py: load module, check API public methods tồn tại.
  4. web/server.py: parse được + import được class SetTunnelConfigRequest.

Chạy: python3 test/syntax_check_tunnel.py
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKED_FILES = [
    "db/repositories.py",
    "web/cloudflare_tunnel.py",
    "web/server.py",
    "cli.py",
    "web/static/settings_panel.js",
    "web/static/index.html",
    "web/static/style.css",
]


def step(label: str, ok: bool, detail: str = "") -> None:
    flag = "[PASS]" if ok else "[FAIL]"
    line = f"{flag} {label}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)
    if not ok:
        sys.exit(1)


def main() -> None:
    # 1. AST parse Python files (skip .js/.html/.css — không parse Python được).
    for rel in CHECKED_FILES:
        path = ROOT / rel
        step(f"file exists: {rel}", path.exists(), str(path))
        if path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"))
                step(f"ast.parse: {rel}", True)
            except SyntaxError as exc:
                step(f"ast.parse: {rel}", False, repr(exc))

    # 2. Whitelist + validator
    from db.repositories import (  # noqa: E402
        _EXACT_KEYS,
        _validate_type_constraint,
        RepositoryError,
    )

    step(
        "tunnel.cloudflare.enabled in _EXACT_KEYS",
        "tunnel.cloudflare.enabled" in _EXACT_KEYS,
    )

    # bool OK
    try:
        _validate_type_constraint("tunnel.cloudflare.enabled", True)
        _validate_type_constraint("tunnel.cloudflare.enabled", False)
        step("validator accepts bool", True)
    except RepositoryError as exc:
        step("validator accepts bool", False, repr(exc))

    # str/int reject
    for bad in ("yes", 1, 0, None):
        try:
            _validate_type_constraint("tunnel.cloudflare.enabled", bad)
            step(f"validator rejects {bad!r}", False, "expected RepositoryError")
        except RepositoryError:
            step(f"validator rejects {bad!r}", True)

    # 3. Tunnel module API
    mod = importlib.import_module("web.cloudflare_tunnel")
    for attr in (
        "CloudflareTunnelManager",
        "CloudflareTunnelError",
        "get_cloudflare_tunnel",
        "_detect_asset",
    ):
        step(f"web.cloudflare_tunnel.{attr} exists", hasattr(mod, attr))

    tunnel = mod.get_cloudflare_tunnel()
    step("get_cloudflare_tunnel returns singleton",
         tunnel is mod.get_cloudflare_tunnel())

    # apply_settings + set_local_endpoint
    tunnel.apply_settings({"tunnel.cloudflare.enabled": False})
    step("apply_settings False", tunnel.enabled is False)
    tunnel.apply_settings({"tunnel.cloudflare.enabled": True})
    step("apply_settings True", tunnel.enabled is True)
    # Reset for safety.
    tunnel.apply_settings({"tunnel.cloudflare.enabled": False})

    tunnel.set_local_endpoint("0.0.0.0", 8083)
    snap = tunnel.to_status_dict()
    step("set_local_endpoint forces loopback",
         snap["local_host"] == "127.0.0.1" and snap["local_port"] == 8083,
         repr(snap))

    # set_local_endpoint với loopback giữ nguyên.
    tunnel.set_local_endpoint("localhost", 9999)
    snap = tunnel.to_status_dict()
    step("set_local_endpoint keeps loopback host",
         snap["local_host"] == "localhost" and snap["local_port"] == 9999)

    # Bad port reject.
    try:
        tunnel.set_local_endpoint("127.0.0.1", 99999)
        step("set_local_endpoint rejects bad port", False)
    except ValueError:
        step("set_local_endpoint rejects bad port", True)

    # 4. _detect_asset không crash trên platform hiện tại.
    asset, is_targz, ext = mod._detect_asset()
    step("_detect_asset returns asset", isinstance(asset, str) and asset.startswith("cloudflared-"),
         f"asset={asset} is_targz={is_targz} ext={ext}")

    # 5. Server module import + endpoint class tồn tại.
    server_mod = importlib.import_module("web.server")
    step("SetTunnelConfigRequest in web.server",
         hasattr(server_mod, "SetTunnelConfigRequest"))

    # Check route đã đăng ký.
    routes = [r.path for r in server_mod.app.routes if hasattr(r, "path")]
    for path in ("/api/tunnel/status", "/api/tunnel/config", "/api/tunnel/restart"):
        step(f"route {path} đã đăng ký", path in routes)

    print("\n[ALL PASS] tunnel feature syntax/smoke check OK", flush=True)


if __name__ == "__main__":
    main()
