"""Check tĩnh cho fix MFA inline enroll (CF-clean).

Mục tiêu: bắt lỗi syntax + đảm bảo các symbol/wiring then chốt còn nguyên,
KHÔNG gọi mạng. In realtime từng bước [PASS]/[FAIL].
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errs: list[str] = []


def _ok(msg: str) -> None:
    print(f"[PASS] {msg}", flush=True)


def _bad(msg: str) -> None:
    print(f"[FAIL] {msg}", flush=True)
    errs.append(msg)


def _parse(rel: str) -> ast.Module | None:
    p = ROOT / rel
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        _ok(f"AST parse OK :: {rel}")
        return tree
    except SyntaxError as exc:
        _bad(f"SyntaxError {rel}: {exc}")
        return None


FILES = [
    "models.py",
    "mfa_phase.py",
    "request_phase.py",
    "browser_phase.py",
    "signup.py",
    "web/manager.py",
    "autoreg/runner.py",
    "cli.py",
    "db/repositories.py",
]

print("== [1/4] Syntax AST parse ==", flush=True)
trees: dict[str, ast.Module] = {}
for i, rel in enumerate(FILES, 1):
    print(f"[{i}/{len(FILES)}] parse {rel}", flush=True)
    t = _parse(rel)
    if t is not None:
        trees[rel] = t


def _func_names(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


print("== [2/4] Symbol mới trong mfa_phase ==", flush=True)
if "mfa_phase.py" in trees:
    names = _func_names(trees["mfa_phase.py"])
    for fn in ("enable_2fa_in_page", "enable_2fa_in_session",
               "_classify_enroll_response", "_build_mfa_result",
               "_page_request", "_page_access_token", "_backend_headers_sync"):
        (_ok if fn in names else _bad)(f"mfa_phase có {fn}")

print("== [3/4] Wiring text-level ==", flush=True)
checks = [
    ("models.py", "mfa_inline", "SignupRequest.mfa_inline"),
    ("models.py", "two_factor_partial", "models two_factor_partial"),
    ("request_phase.py", "enable_2fa_in_session", "request_phase gọi enable_2fa_in_session"),
    ("request_phase.py", "\"two_factor\":", "request_phase trả two_factor"),
    ("browser_phase.py", "enable_2fa_in_page", "browser_phase gọi enable_2fa_in_page"),
    ("browser_phase.py", "two_factor=mfa_holder.get", "browser_phase build handoff.two_factor"),
    ("signup.py", "result.two_factor = handoff.two_factor", "signup propagate handoff.two_factor"),
    ("web/manager.py", "mfa_result = result.two_factor", "manager tiêu thụ inline"),
    ("web/manager.py", "self._mfa_inline", "manager hydrate _mfa_inline"),
    ("web/manager.py", "request.mfa_inline = self._mfa_inline", "manager set request.mfa_inline"),
    ("autoreg/runner.py", "mfa_result = result.two_factor", "autoreg tiêu thụ inline"),
    ("cli.py", "mfa_inline=False", "cli signup tắt inline"),
    ("db/repositories.py", "reg.mfa_inline", "repositories whitelist + constraint"),
]
for rel, needle, label in checks:
    src = (ROOT / rel).read_text(encoding="utf-8")
    (_ok if needle in src else _bad)(label)

print("== [4/4] reg.mfa_inline trong _EXACT_KEYS + bool constraint ==", flush=True)
repo_src = (ROOT / "db/repositories.py").read_text(encoding="utf-8")
in_whitelist = '"reg.mfa_inline"' in repo_src
in_bool = "reg.mfa_inline" in repo_src.split("_validate_type_constraint", 1)[-1]
(_ok if in_whitelist else _bad)("reg.mfa_inline trong _EXACT_KEYS")
(_ok if in_bool else _bad)("reg.mfa_inline có type constraint")

print("=" * 50, flush=True)
if errs:
    print(f"RESULT: FAIL ({len(errs)} lỗi)", flush=True)
    for e in errs:
        print(f"  - {e}", flush=True)
    sys.exit(1)
print("RESULT: ALL PASS", flush=True)
