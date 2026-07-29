"""Task 1.1 verify — auth_session_logging_id đọc từ cookie `oai-asli`.

Mục tiêu:
    - Verify helpers `read_oai_asli_from_ctx` (async) + `read_oai_asli_from_session`
      (sync) trong `_nextauth_bootstrap.py` hoạt động đúng.
    - Verify caller `browser_phase.run_browser_phase`, `session_phase._drive_session_flow`,
      và session_phase anti409 flow đã import + dùng helper (grep AST/source).

Chạy: python3 test/check_logging_id_consistency.py
"""
from __future__ import annotations

import asyncio
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────
# 1. Helpers behavior
# ─────────────────────────────────────────────────────────────────────


class _FakeAsyncCtx:
    """Mock Camoufox/Playwright context. cookies(url) returns list[{name,value}]."""

    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    async def cookies(self, url: str = "") -> list[dict]:  # noqa: ARG002
        return list(self._cookies)


class _FakeAsyncCtxRaising:
    async def cookies(self, url: str = "") -> list[dict]:  # noqa: ARG002
        raise RuntimeError("ctx closed")


class _FakeSyncSession:
    class _Jar:
        def __init__(self, mapping: dict[str, str]) -> None:
            self._m = mapping

        def get(self, name: str, default=None):
            return self._m.get(name, default)

    def __init__(self, mapping: dict[str, str]) -> None:
        self.cookies = self._Jar(mapping)


class _FakeSessionRaising:
    class _Jar:
        def get(self, name, default=None):
            raise RuntimeError("jar broken")

    def __init__(self) -> None:
        self.cookies = self._Jar()


async def _check_async() -> None:
    from _nextauth_bootstrap import read_oai_asli_from_ctx

    # Case 1: cookie present
    ctx = _FakeAsyncCtx([
        {"name": "other", "value": "x"},
        {"name": "oai-asli", "value": "abc-123-def"},
    ])
    got = await read_oai_asli_from_ctx(ctx)
    assert got == "abc-123-def", f"async cookie present → got {got!r}"
    print("[PASS] async: cookie present → returns value")

    # Case 2: cookie missing
    ctx = _FakeAsyncCtx([{"name": "other", "value": "x"}])
    got = await read_oai_asli_from_ctx(ctx)
    assert got is None, f"async cookie missing → got {got!r}"
    print("[PASS] async: cookie missing → None")

    # Case 3: cookie empty value
    ctx = _FakeAsyncCtx([{"name": "oai-asli", "value": ""}])
    got = await read_oai_asli_from_ctx(ctx)
    assert got is None, f"async cookie empty → got {got!r}"
    print("[PASS] async: cookie empty value → None")

    # Case 4: ctx raises → returns None (best-effort)
    ctx = _FakeAsyncCtxRaising()
    got = await read_oai_asli_from_ctx(ctx)
    assert got is None, f"async ctx raise → got {got!r}"
    print("[PASS] async: ctx exception → None")


def _check_sync() -> None:
    from _nextauth_bootstrap import read_oai_asli_from_session

    sess = _FakeSyncSession({"oai-asli": "xyz-789", "other": "z"})
    got = read_oai_asli_from_session(sess)
    assert got == "xyz-789", f"sync cookie present → got {got!r}"
    print("[PASS] sync: cookie present → returns value")

    sess = _FakeSyncSession({"other": "z"})
    got = read_oai_asli_from_session(sess)
    assert got is None, f"sync cookie missing → got {got!r}"
    print("[PASS] sync: cookie missing → None")

    sess = _FakeSyncSession({"oai-asli": ""})
    got = read_oai_asli_from_session(sess)
    assert got is None, f"sync cookie empty → got {got!r}"
    print("[PASS] sync: cookie empty value → None")

    sess = _FakeSessionRaising()
    got = read_oai_asli_from_session(sess)
    assert got is None, f"sync session raise → got {got!r}"
    print("[PASS] sync: session exception → None")


# ─────────────────────────────────────────────────────────────────────
# 2. Source-level invariants — caller dùng helper, không gen UUID standalone
# ─────────────────────────────────────────────────────────────────────


_BAD_PATTERNS_BROWSER_PHASE = [
    # Trước fix: `logging_id = str(uuid.uuid4())` ngay sau device_id ở
    # run_browser_phase scope. Sau fix: chỉ còn trong helper local
    # `_resolve_logging_id` hoặc fallback path.
    re.compile(r"^\s+logging_id = str\(uuid\.uuid4\(\)\)\s*$", re.MULTILINE),
]


def _check_browser_phase_source() -> None:
    src = (ROOT / "browser_phase.py").read_text(encoding="utf-8")

    # Phải import helper
    assert "read_oai_asli_from_ctx" in src, \
        "browser_phase.py phải import read_oai_asli_from_ctx"
    print("[PASS] browser_phase.py imports read_oai_asli_from_ctx")

    # Phải có _resolve_logging_id helper
    assert "_resolve_logging_id" in src, \
        "browser_phase.py thiếu helper _resolve_logging_id"
    print("[PASS] browser_phase.py defines _resolve_logging_id helper")

    # KHÔNG còn `logging_id = str(uuid.uuid4())` ở top scope của runner
    # (chỉ allowed bên trong helper hoặc inline fallback đi kèm comment)
    # Heuristic: search line `    logging_id = str(uuid.uuid4())` (4 space)
    bad_lines = []
    for ln, line in enumerate(src.splitlines(), 1):
        if re.match(r"^    logging_id = str\(uuid\.uuid4\(\)\)\s*$", line):
            bad_lines.append((ln, line))
    assert not bad_lines, (
        f"browser_phase.py còn line gen logging_id ở scope cao: {bad_lines}"
    )
    print("[PASS] browser_phase.py: no top-scope `logging_id = str(uuid.uuid4())`")


def _check_session_phase_source() -> None:
    src = (ROOT / "session_phase.py").read_text(encoding="utf-8")

    # Phải import helper (cả async + sync)
    assert "read_oai_asli_from_ctx" in src, \
        "session_phase.py phải import read_oai_asli_from_ctx"
    print("[PASS] session_phase.py imports read_oai_asli_from_ctx")

    assert "read_oai_asli_from_session" in src, \
        "session_phase.py phải import read_oai_asli_from_session"
    print("[PASS] session_phase.py imports read_oai_asli_from_session")

    # KHÔNG còn `auth_session_logging_id: str(__import__('uuid').uuid4())`
    # standalone trong _au_params (anti409 flow). Sau fix: gọi qua _read_asli_sync.
    assert (
        '"auth_session_logging_id": str(__import__(\'uuid\').uuid4())' not in src
        and '"auth_session_logging_id": str(__import__("uuid").uuid4())' not in src
    ), "session_phase.py: auth_session_logging_id vẫn gen UUID standalone trong _au_params"
    print("[PASS] session_phase.py: anti409 dùng _read_asli_sync, không gen UUID standalone")


def _check_nextauth_bootstrap_source() -> None:
    src = (ROOT / "_nextauth_bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "read_oai_asli_from_ctx" in funcs, "missing async helper"
    assert "read_oai_asli_from_session" in funcs, "missing sync helper"
    print("[PASS] _nextauth_bootstrap.py exports both helpers")


def main() -> int:
    print("=" * 70)
    print("  Task 1.1 — auth_session_logging_id ↔ oai-asli cookie consistency")
    print("=" * 70)
    print()

    asyncio.run(_check_async())
    print()
    _check_sync()
    print()
    _check_browser_phase_source()
    _check_session_phase_source()
    _check_nextauth_bootstrap_source()
    print()
    print("[OK] All Task 1.1 invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
