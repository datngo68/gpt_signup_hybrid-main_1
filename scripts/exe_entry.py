"""PyInstaller entrypoint cho `GSH.exe`.

Khác với ``__main__.py`` (chạy qua ``python -m gpt_signup_hybrid``), exe khởi
động qua double-click — không có argv, không có terminal. Mặc định:

  1. Enforce expire date (block sớm nếu hết hạn).
  2. Set ``PLAYWRIGHT_BROWSERS_PATH`` trỏ vào folder bundled cùng exe (Chromium
     đã được CI build copy vào ``_internal/playwright_browsers`` khi --onefile
     thì PyInstaller tự extract sang ``sys._MEIPASS``).
  3. Khởi động lệnh ``web`` mặc định (uvicorn + tab default).

User vẫn có thể truyền argv (qua shortcut hoặc terminal) để override:
  GSH.exe pool-status pool.txt
  GSH.exe web --port 9000
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _resource_path(rel: str) -> Path:
    """Resolve path tới resource bundled với exe.

    PyInstaller --onefile: resource extract ra ``sys._MEIPASS`` (temp dir).
    Dev mode: relative tới script này.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent.parent / rel


def _setup_playwright_browsers_path() -> None:
    """Trỏ Playwright vào folder Chromium bundled cùng exe.

    CI sẽ copy ``%LOCALAPPDATA%\\ms-playwright\\chromium-*`` vào
    ``playwright-browsers/`` trước khi PyInstaller build, và spec sẽ include
    folder đó vào exe. Runtime: trỏ env var để Playwright skip download.
    """
    bundled = _resource_path("playwright-browsers")
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)


def _setup_runtime_dir() -> None:
    """Đặt ``runtime/`` cạnh exe (không trong _MEIPASS — temp dir bị xoá).

    SQLite DB + QR cache sẽ persist theo thư mục cài đặt. Khi chạy onefile,
    cwd có thể là cwd của user; chuyển sang folder exe để DB ổn định.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        os.chdir(exe_dir)


def main() -> None:
    _setup_runtime_dir()

    # 1. Expire check NGAY ĐẦU. Tách import vào trong main() để dev mode (chạy
    # script này trực tiếp) cũng work.
    from _expire_check import enforce_expiry  # type: ignore[import-not-found]
    enforce_expiry()

    # 2. Bundled Chromium
    _setup_playwright_browsers_path()

    # 3. Default args: nếu user double-click không có argv → mặc định ``web``.
    if len(sys.argv) <= 1:
        sys.argv = [sys.argv[0], "web"]

    # 4. Forward sang typer CLI. Import lazy để expire block không kéo theo
    # uvicorn / fastapi.
    from cli import app  # type: ignore[import-not-found]
    app()


if __name__ == "__main__":
    main()
