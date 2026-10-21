from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "session_phase.py"


def main() -> None:
    ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    sys.path.insert(0, str(ROOT))
    import session_phase

    parsed = session_phase._parse_codex_quota(
        {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 25, "reset_at": "2026-08-14T12:00:00Z"},
                "secondary_window": {"percent_used": 60, "resets_at": "2026-08-21T12:00:00Z"},
            },
            "rate_limit_reset_credits": {"available_count": 2},
        }
    )
    assert parsed["plan"] == "plus"
    assert parsed["quotas"]["session"]["remaining"] == 75.0
    assert parsed["quotas"]["weekly"]["used"] == 60.0
    assert parsed["reset_credits"]["available_count"] == 2


if __name__ == "__main__":
    main()
