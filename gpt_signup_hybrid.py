"""Repo-root module shim so `python -m gpt_signup_hybrid` works from this directory.

Khi cwd chính là thư mục package `gpt_signup_hybrid/`, Python không tìm được
package cùng tên qua import machinery mặc định. File shim này biến chính thư mục
hiện tại thành package runtime tối thiểu và forward vào root CLI.
"""
from __future__ import annotations

import importlib
from pathlib import Path

# Expose current directory as package search path so relative imports in
# `gpt_signup_hybrid.cli` continue to work.
__path__ = [str(Path(__file__).resolve().parent)]
__package__ = __name__


def main() -> None:
    # Enforce expire ngay trước khi load CLI — block sớm khi exe expired
    # mà không phải đợi typer/uvicorn import.
    from _expire_check import enforce_expiry
    enforce_expiry()
    cli_mod = importlib.import_module(".cli", __name__)
    cli_mod.app()


if __name__ == "__main__":
    main()
