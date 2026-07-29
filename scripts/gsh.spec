# PyInstaller spec — GSH Windows onefile build.
#
# Run via: scripts/build_exe.py (KHÔNG chạy spec trực tiếp).
#
# Cấu trúc:
#   - Entry: scripts/exe_entry.py (đã setup expire check + Playwright path
#     trước khi forward sang typer CLI).
#   - Source files: gom toàn bộ .py ở project root + sub-packages thành
#     module top-level (PyInstaller --onefile yêu cầu phẳng, không nested
#     package vì entry không phải `python -m pkg`).
#   - Hidden imports: curl_cffi, playwright, uvicorn, fastapi extras —
#     PyInstaller analyzer thường bỏ sót do các module load lazy.
#   - Datas: web/static/* (HTML/CSS/JS), playwright-browsers/ (Chromium nếu
#     có), _expire_const.py (generated).
# noqa: E501
from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


PROJECT_ROOT = Path(os.environ.get("GSH_BUILD_ROOT", ".")).resolve()
ENTRY = str(PROJECT_ROOT / "scripts" / "exe_entry.py")


# --- Collect deps có native lib / lazy load ---
binaries = []
hiddenimports = []
datas = []

for pkg in ("curl_cffi", "playwright", "playwright_stealth", "uvicorn", "fastapi"):
    try:
        d, b, hi = collect_all(pkg)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(hi)
    except Exception:  # noqa: BLE001
        pass

# Hidden imports thường bị miss — manual khai báo:
hiddenimports.extend([
    "curl_cffi.requests",
    "curl_cffi.curl",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.loops",
    "uvicorn.loops.asyncio",
    "uvicorn.logging",
    "fastapi",
    "fastapi.responses",
    "fastapi.staticfiles",
    "starlette",
    "starlette.routing",
    "pyotp",
    "qrcode",
    "qrcode.image.pil",
    "PIL",
    "PIL.Image",
    "playwright._impl._driver",
    "playwright.async_api",
    "playwright.sync_api",
    "playwright_stealth",
    "h11",
    "anyio",
])


# --- Collect source files (root-level modules) ---
# Map từng .py file của project root + sub-packages thành module top-level.
# Cách này đơn giản hơn maintain manifest; trade-off: thêm .py không liên
# quan vào exe (nhỏ, chấp nhận được).
SOURCE_DIRS = ["", "web", "db", "autoreg", "codex_auth", "icloud_hme", "scripts"]
EXCLUDE_FILES = {
    "__main__.py",  # entry script kiểu `python -m` — không dùng cho exe
    "conftest.py",
}

for sub in SOURCE_DIRS:
    src_dir = PROJECT_ROOT / sub if sub else PROJECT_ROOT
    if not src_dir.is_dir():
        continue
    for py in src_dir.glob("*.py"):
        if py.name in EXCLUDE_FILES:
            continue
        # Đặt vào root namespace của exe (PyInstaller datas dest='.')
        rel_dest = "." if sub == "" else sub
        datas.append((str(py), rel_dest))

# Static web files (HTML/CSS/JS) — bundled vào exe; runtime đọc qua
# `Path(sys._MEIPASS) / 'web' / 'static'`. server.py đã handle qua
# `Path(__file__).resolve().parent / 'static'` — sẽ work trong _MEIPASS.
static_dir = PROJECT_ROOT / "web" / "static"
if static_dir.is_dir():
    for f in static_dir.iterdir():
        if f.is_file():
            datas.append((str(f), "web/static"))


# --- Playwright browsers bundle (CI populate trước khi build) ---
# CI step copy %LOCALAPPDATA%\ms-playwright\chromium-* vào playwright-browsers/
# trước khi gọi PyInstaller. exe_entry.py set PLAYWRIGHT_BROWSERS_PATH trỏ vào
# folder này tại runtime.
browsers_dir = PROJECT_ROOT / "playwright-browsers"
if browsers_dir.is_dir():
    for root, _dirs, files in os.walk(browsers_dir):
        rel_root = Path(root).relative_to(PROJECT_ROOT)
        for f in files:
            datas.append((str(Path(root) / f), str(rel_root)))


# --- Generated expire constant ---
expire_const = PROJECT_ROOT / "_expire_const.py"
if expire_const.is_file():
    datas.append((str(expire_const), "."))


block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[
        str(PROJECT_ROOT),
        str(PROJECT_ROOT / "scripts"),
    ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "test",
        "tests",
        "pytest",
        "matplotlib",
        "numpy.testing",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="GSH",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX gây false-positive AV trên Windows
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # giữ console để user thấy expire message + uvicorn log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "scripts" / "gsh.ico") if (PROJECT_ROOT / "scripts" / "gsh.ico").is_file() else None,
)
