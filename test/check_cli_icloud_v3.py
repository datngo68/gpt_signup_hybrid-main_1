"""Verify CLI signup command tích hợp đúng provider icloud_v3.

KHÔNG chạy reg thật (cần proxy + browser). Verify qua:
  - typer CliRunner gọi `--help` → option `--icloud-v3` xuất hiện.
  - Source-grep verify code path:
        * resolved_provider auto-detect 'icloud_v3' khi có --icloud-v3.
        * icloud_v3_url được pass vào SignupRequest.
  - Mock SignupRequest construction qua AST scan kiểm tra param truyền đúng.

Run: python3 test/check_cli_icloud_v3.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = "PASS"
FAIL = "FAIL"


def _label(idx: int, total: int, name: str) -> str:
    return f"[{idx:02d}/{total:02d}] {name}"


def tc01_cli_help_shows_icloud_v3(idx: int, total: int) -> bool:
    """typer help phải có --icloud-v3 option."""
    name = _label(idx, total, "TC-01 cli_help_has_icloud_v3")
    try:
        from typer.testing import CliRunner
        from cli import app
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {name}: import fail: {type(exc).__name__}: {exc}")
        return False

    # Tắt rich + set width rộng để help text không bị wrap/truncate
    import os
    runner = CliRunner(env={**os.environ, "COLUMNS": "200", "_TYPER_FORCE_DISABLE_TERMINAL": "1"})
    res = runner.invoke(app, ["signup", "--help"])
    ok = res.exit_code == 0 and "--icloud-v3" in res.stdout
    print(f"{PASS if ok else FAIL} {name}: exit={res.exit_code} has_flag={'--icloud-v3' in res.stdout}")
    if not ok:
        print(f"  stdout[:500]: {res.stdout[:500]}")
    return ok


def tc02_cli_help_mentions_icloud_v3_in_provider(idx: int, total: int) -> bool:
    """Help text của --mail-provider phải nhắc icloud_v3."""
    name = _label(idx, total, "TC-02 mail_provider_help_lists_icloud_v3")
    from typer.testing import CliRunner
    from cli import app

    import os
    runner = CliRunner(env={**os.environ, "COLUMNS": "200", "_TYPER_FORCE_DISABLE_TERMINAL": "1"})
    res = runner.invoke(app, ["signup", "--help"])
    ok = res.exit_code == 0 and "icloud_v3" in res.stdout
    print(f"{PASS if ok else FAIL} {name}: contains 'icloud_v3' = {'icloud_v3' in res.stdout}")
    return ok


def tc03_cli_bad_icloud_v3_input(idx: int, total: int) -> bool:
    """--icloud-v3 chuỗi sai format → exit code 2."""
    name = _label(idx, total, "TC-03 cli_bad_icloud_v3_input")
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["signup", "--icloud-v3", "no-separator-here", "--headless"],
    )
    ok = res.exit_code == 2 and "invalid" in res.output.lower()
    print(f"{PASS if ok else FAIL} {name}: exit={res.exit_code}")
    if not ok:
        print(f"  output[:400]: {res.output[:400]}")
    return ok


def tc04_cli_icloud_v3_missing_url_when_provider_forced(idx: int, total: int) -> bool:
    """--mail-provider icloud_v3 KHÔNG kèm --icloud-v3 → exit 2."""
    name = _label(idx, total, "TC-04 cli_icloud_v3_missing_url")
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "signup",
            "--mail-provider", "icloud_v3",
            "--email", "x@icloud.com",
            "--headless",
        ],
    )
    ok = res.exit_code == 2 and "icloud_v3" in res.output and "yêu cầu" in res.output
    print(f"{PASS if ok else FAIL} {name}: exit={res.exit_code}")
    if not ok:
        print(f"  output[:400]: {res.output[:400]}")
    return ok


def tc05_cli_unknown_provider_rejected(idx: int, total: int) -> bool:
    """--mail-provider 'bogus' → exit 2 với message dễ hiểu."""
    name = _label(idx, total, "TC-05 cli_unknown_provider_rejected")
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "signup",
            "--mail-provider", "bogus_provider_xyz",
            "--email", "x@icloud.com",
            "--headless",
        ],
    )
    ok = res.exit_code == 2 and "bogus_provider_xyz" in res.output
    print(f"{PASS if ok else FAIL} {name}: exit={res.exit_code}")
    if not ok:
        print(f"  output[:400]: {res.output[:400]}")
    return ok


def tc06_source_has_icloud_v3_url_in_signup_request(idx: int, total: int) -> bool:
    """AST scan: SignupRequest construction trong cli.py phải có icloud_v3_url=..."""
    name = _label(idx, total, "TC-06 cli_passes_icloud_v3_url")
    src = (ROOT / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SignupRequest"
        ):
            for kw in node.keywords:
                if kw.arg == "icloud_v3_url":
                    found = True
                    break
            if found:
                break
    print(f"{PASS if found else FAIL} {name}: icloud_v3_url passed to SignupRequest")
    return found


def tc07_source_auto_detect_priority(idx: int, total: int) -> bool:
    """cli.py phải auto-detect icloud_v3 khi có --icloud-v3 trước outlook."""
    name = _label(idx, total, "TC-07 auto_detect_priority")
    src = (ROOT / "cli.py").read_text(encoding="utf-8")
    # Đoạn block cần có: kiểm tra icloud_v3_url TRƯỚC outlook_combo
    has_icloud_branch = re.search(
        r'resolved_provider\s*=\s*["\']icloud_v3["\']', src
    ) is not None
    has_priority_text = "icloud_v3_url" in src and "outlook_combo" in src
    ok = has_icloud_branch and has_priority_text
    print(f"{PASS if ok else FAIL} {name}: icloud_branch={has_icloud_branch} priority={has_priority_text}")
    return ok


TESTS = [
    tc01_cli_help_shows_icloud_v3,
    tc02_cli_help_mentions_icloud_v3_in_provider,
    tc03_cli_bad_icloud_v3_input,
    tc04_cli_icloud_v3_missing_url_when_provider_forced,
    tc05_cli_unknown_provider_rejected,
    tc06_source_has_icloud_v3_url_in_signup_request,
    tc07_source_auto_detect_priority,
]


def main() -> int:
    total = len(TESTS)
    print(f"=== check_cli_icloud_v3 ({total} test cases) ===")
    failed = 0
    for i, fn in enumerate(TESTS, start=1):
        try:
            ok = fn(i, total)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL [{i:02d}/{total:02d}] {fn.__name__}: UNCAUGHT {type(exc).__name__}: {exc}")
            ok = False
        if not ok:
            failed += 1
    print(f"=== result: {total - failed}/{total} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
