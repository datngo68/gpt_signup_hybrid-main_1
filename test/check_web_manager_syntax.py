from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ast.parse((ROOT / "web" / "manager.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
