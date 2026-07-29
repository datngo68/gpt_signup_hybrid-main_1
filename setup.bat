@echo off
REM gpt_signup_hybrid — 1 lệnh setup + start web UI (Windows)
REM Coi thư mục này là project root: .venv, runtime, .env đều nằm
REM trong gpt_signup_hybrid/, không leak ra parent.
REM
REM Usage: double-click setup.bat hoặc chạy trong cmd/powershell.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "ROOT_DIR=%CD%"
set "PKG_NAME=gpt_signup_hybrid"

echo ═══════════════════════════════════════════════════════════
echo   gpt_signup_hybrid — auto setup + start (Windows)
echo   root:   %ROOT_DIR%
echo ═══════════════════════════════════════════════════════════
echo.

REM 1. Python venv trong chính package
if not exist ".venv" (
    echo [1/6] Creating .venv...
    python -m venv .venv
) else (
    echo [1/6] .venv exists √
)

REM 2. Install deps
echo [2/6] Installing dependencies...
.venv\Scripts\pip install -q --upgrade pip 2>nul
.venv\Scripts\pip install -q pydantic typer httpx "curl_cffi>=0.7" pyotp fastapi uvicorn camoufox playwright ruyiPage 2>nul

REM 3. Shim dir + junction + .pth để import package bất kể tên folder
echo [3/6] Wiring package import via shim junction...
for /f "delims=" %%i in ('.venv\Scripts\python -c "import site; print(site.getsitepackages()[0])" 2^>nul') do set "SITE_PKG=%%i"
if defined SITE_PKG (
    set "SHIM_DIR=%SITE_PKG%\_gpt_signup_hybrid_shim"
    if not exist "!SHIM_DIR!" mkdir "!SHIM_DIR!"
    set "SHIM_LINK=!SHIM_DIR!\%PKG_NAME%"
    if exist "!SHIM_LINK!" rmdir "!SHIM_LINK!" 2>nul
    mklink /J "!SHIM_LINK!" "%ROOT_DIR%" >nul
    echo !SHIM_DIR!> "%SITE_PKG%\_gpt_signup_hybrid_root.pth"
    echo   √ junction !SHIM_LINK! → %ROOT_DIR%
    echo   √ pth      %SITE_PKG%\_gpt_signup_hybrid_root.pth
) else (
    echo   ERROR: không xác định được site-packages.
    exit /b 1
)

REM 4. Playwright Firefox
echo [4/6] Installing Playwright Firefox...
.venv\Scripts\playwright install firefox 2>nul

REM 5. Camoufox binary
echo [5/6] Fetching Camoufox binary...
.venv\Scripts\python -m camoufox fetch 2>nul

REM 6. .env
if not exist ".env" (
    echo [6/6] Creating .env...
    (
        echo BROWSER_ENGINE=camoufox
        echo RUNTIME_DIR=runtime
        echo BROWSER_VIEWPORT_WIDTH=1440
        echo BROWSER_VIEWPORT_HEIGHT=800
        echo BROWSER_USE_PROFILE_TEMPLATE=true
        echo BROWSER_PROFILE_TEMPLATE_DIR=runtime/profiles/template
        echo BROWSER_CAMOUFOX_PROFILE_DIR=runtime/profiles/camoufox_template
        echo HYBRID_MAX_CONCURRENT=2
        echo HYBRID_OUTLOOK_PROXY=
        echo HYBRID_JOB_TIMEOUT=240
    ) > .env
    echo   √ .env created
) else (
    echo [6/6] .env exists √
)

REM Tạo runtime dirs
if not exist "runtime\profiles\template" mkdir "runtime\profiles\template"
if not exist "runtime\profiles\camoufox_template" mkdir "runtime\profiles\camoufox_template"
if not exist "runtime\sessions" mkdir "runtime\sessions"
if not exist "runtime\outlook_state" mkdir "runtime\outlook_state"
if not exist "runtime\outlook_pool" mkdir "runtime\outlook_pool"
if not exist "runtime\har_hybrid" mkdir "runtime\har_hybrid"

echo.
echo ═══════════════════════════════════════════════════════════
echo   √ Setup done. Starting web UI...
echo   → http://127.0.0.1:8083/
echo.
echo   Paste combo vao textarea + bam Run.
echo   Format: email^|password^|refresh_token^|client_id
echo ═══════════════════════════════════════════════════════════
echo.

.venv\Scripts\python -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083

pause
