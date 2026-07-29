"""Verify fix /about-you: điền NĂM SINH (year-of-birth) thay vì tuổi + default
birthdate 11/11/1999.

Chạy:
    python3 test/check_about_you_birth.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from _human_input import age_from_birthdate, resolve_birth_field_value  # noqa: E402
import random_profile  # noqa: E402


class FakeLocator:
    """Giả Playwright Locator: ``evaluate`` trả về meta input cố định."""

    def __init__(self, meta: dict):
        self._meta = meta

    async def evaluate(self, _js: str):
        return self._meta


_FAILS: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[PASS] {name}", flush=True)
    else:
        print(f"[FAIL] {name} — {detail}", flush=True)
        _FAILS.append(name)


async def _run_resolver_cases() -> None:
    bd = "1999-11-11"
    today = date(2026, 6, 27)

    # 1. Year-of-birth field (đúng bug: min 1896, max 2013) → điền NĂM "1999".
    val, kind = await resolve_birth_field_value(
        FakeLocator({"min": "1896", "max": "2013", "name": "age",
                     "placeholder": "", "ariaLabel": ""}),
        bd, today=today,
    )
    _check("year field (min 1896) → year 1999",
           val == "1999" and kind == "year", f"got {val!r}/{kind!r}")

    # 2. Age field thật (min 1, max 120) → điền TUỔI.
    expected_age = str(age_from_birthdate(bd, today=today))  # 26 (chưa qua 11/11)
    val, kind = await resolve_birth_field_value(
        FakeLocator({"min": "1", "max": "120", "name": "age",
                     "placeholder": "", "ariaLabel": ""}),
        bd, today=today,
    )
    _check("age field (min 1 max 120) → age",
           val == expected_age and kind == "age", f"got {val!r}/{kind!r}")

    # 3. Không có min/max → mặc định an toàn = year (UI hiện tại).
    val, kind = await resolve_birth_field_value(
        FakeLocator({"min": "", "max": "", "name": "age",
                     "placeholder": "", "ariaLabel": ""}),
        bd, today=today,
    )
    _check("no min/max → fallback year",
           val == "1999" and kind == "year", f"got {val!r}/{kind!r}")

    # 4. Nhãn nhắc "Year of birth" dù min trống → year.
    val, kind = await resolve_birth_field_value(
        FakeLocator({"min": "", "max": "", "name": "",
                     "placeholder": "Year of birth", "ariaLabel": ""}),
        bd, today=today,
    )
    _check("placeholder 'Year of birth' → year",
           val == "1999" and kind == "year", f"got {val!r}/{kind!r}")

    # 5. evaluate raise (locator chết) → vẫn fallback year, không crash.
    class BrokenLocator:
        async def evaluate(self, _js):
            raise RuntimeError("detached")

    val, kind = await resolve_birth_field_value(BrokenLocator(), bd, today=today)
    _check("evaluate raise → fallback year (no crash)",
           val == "1999" and kind == "year", f"got {val!r}/{kind!r}")


def _check_age_rule() -> None:
    # 11/11/1999, hôm nay 27/06/2026 → chưa qua sinh nhật → 26.
    _check("age_from_birthdate 1999-11-11 @2026-06-27 == 26",
           age_from_birthdate("1999-11-11", today=date(2026, 6, 27)) == 26)
    # Sau sinh nhật (12/11/2026) → 27.
    _check("age_from_birthdate 1999-11-11 @2026-12-12 == 27",
           age_from_birthdate("1999-11-11", today=date(2026, 12, 12)) == 27)


def _check_default_birthdate() -> None:
    _check("DEFAULT_BIRTH_YEAR == 1999",
           random_profile.DEFAULT_BIRTH_YEAR == 1999)

    # Sinh nhiều lần để chắc day/month luôn trong [1,12] (swap-safe).
    for _ in range(200):
        bd = random_profile._default_birthdate()
        y, m, d = (int(x) for x in bd.split("-"))
        if not (y == 1999 and 1 <= m <= 12 and 1 <= d <= 12):
            _check("_default_birthdate swap-safe (year 1999, m/d ≤ 12)",
                   False, f"got {bd!r}")
            break
    else:
        _check("_default_birthdate swap-safe (year 1999, m/d ≤ 12)", True)

    p = random_profile.random_profile()
    py, pm, pd = (int(x) for x in p["birthdate"].split("-"))
    _check("random_profile birthdate year 1999 + m/d ≤ 12",
           py == 1999 and 1 <= pm <= 12 and 1 <= pd <= 12,
           f"got {p['birthdate']!r}")
    _check("random_profile age khớp birthdate",
           p["age"] == random_profile._age_from_birthdate(p["birthdate"]),
           f"got age={p['age']}")
    _check("random_profile có name + password",
           bool(p["name"]) and bool(p["password"]))

    ip = random_profile.random_india_profile()
    iy, im, idd = (int(x) for x in ip["birthdate"].split("-"))
    _check("random_india_profile birthdate year 1999 + m/d ≤ 12",
           iy == 1999 and 1 <= im <= 12 and 1 <= idd <= 12,
           f"got {ip['birthdate']!r}")

    lp = random_profile.random_profile_for_locale("en-IN")
    ly, lm, ld = (int(x) for x in lp["birthdate"].split("-"))
    _check("random_profile_for_locale(en-IN) year 1999 + m/d ≤ 12",
           ly == 1999 and 1 <= lm <= 12 and 1 <= ld <= 12,
           f"got {lp['birthdate']!r}")


def _check_syntax() -> None:
    files = [
        "_human_input.py",
        "browser_phase.py",
        "sentinel_sidecar.py",
        "random_profile.py",
    ]
    for rel in files:
        path = ROOT / rel
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            _check(f"AST parse {rel}", True)
        except SyntaxError as exc:
            _check(f"AST parse {rel}", False, f"{exc}")


def main() -> int:
    asyncio.run(_run_resolver_cases())
    _check_age_rule()
    _check_default_birthdate()
    _check_syntax()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAIL: {_FAILS}", flush=True)
        return 1
    print("\nALL PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
