"""CLI cho gpt_signup_hybrid.

Usage:
    .venv/bin/python -m gpt_signup_hybrid signup --email foo@icloud.com
    .venv/bin/python -m gpt_signup_hybrid signup --email foo@icloud.com \
        --logs-url https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs \
        --api-key 12345678@ \
        --name "John Doe" --birthdate 1995-03-15
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import typer

from config import env_insecure_tls, load_settings, runtime_session_dir
from models import SignupRequest, SignupResult
from signup import run_signup

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _emit_log(prefix: str | None = None):
    def log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        head = f"[{ts}]"
        if prefix:
            head += f"[{prefix}]"
        typer.echo(f"{head} {msg}")
    return log


@app.command("pool-status")
def pool_status_cmd(
    pool_file: Path = typer.Argument(..., help="Path tới pool file."),
    db_path: str = typer.Option(
        "runtime/data.db",
        "--db-path",
        help="Đường dẫn tới SQLite database file.",
    ),
) -> None:
    """In tóm tắt pool: bao nhiêu combo đã used / available / terminal error."""
    settings = load_settings()
    from outlook_pool import OutlookPoolError, parse_pool_file, status_summary

    pool_path = Path(pool_file)
    if not pool_path.is_absolute():
        pool_path = settings.root_dir / pool_path

    try:
        pool = parse_pool_file(pool_path)
    except OutlookPoolError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    # Prefer SQLite as source of truth; fallback to JSON state files
    combo_repo = None
    try:
        import atexit as _atexit_ps
        from db import get_engine, get_repos

        engine = get_engine(db_path)
        _atexit_ps.register(engine.close)
        combo_repo, _, _ = get_repos(engine)
    except Exception:
        pass  # DB not available, fallback to JSON

    state_dir = settings.runtime_dir / "outlook_state"
    summary = status_summary(pool, state_dir=state_dir, combo_repo=combo_repo)
    typer.echo(json.dumps({"pool": str(pool_path), **summary}, indent=2))


@app.command("totp")
def totp_cmd(
    secret: str = typer.Argument(..., help="Base32 secret từ /mfa/enroll. VD: B2P3OQCCXINLHGPUDIS55DHQDW5MENK5"),
    account: str | None = typer.Option(None, "--account", help="Email account để in provisioning URI (tuỳ chọn)."),
) -> None:
    """Gen 6-digit TOTP code từ secret base32."""
    from totp_helper import TotpError, generate_code, provisioning_uri, time_remaining

    try:
        code = generate_code(secret)
    except TotpError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    out: dict[str, str | int] = {
        "code": code,
        "valid_for_seconds": time_remaining(),
    }
    if account:
        out["provisioning_uri"] = provisioning_uri(secret, account=account)
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))


@app.command("enable-2fa")
def enable_2fa_cmd(
    session_file: Path = typer.Option(..., "--session-file", "-f", help="SignupResult JSON file (chứa access_token)."),
    activate: bool = typer.Option(True, "--activate/--enroll-only", help="Activate luôn (gen+verify code) hay chỉ enroll lấy secret."),
    proxy: str | None = typer.Option(None, "--proxy", help="HTTP/HTTPS proxy."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Lưu kết quả 2FA. Default: <session-file>.2fa.json"),
    no_file_output: bool = typer.Option(False, "--no-file-output", help="Skip JSON file creation, chỉ persist vào SQLite."),
    db_path: str = typer.Option("runtime/data.db", "--db-path", help="Đường dẫn tới SQLite database file."),
) -> None:
    """Enable 2FA TOTP cho account đã đăng ký. Cần access_token từ SignupResult.

    Output gồm secret base32, provisioning_uri (cho Authenticator), first_code,
    factor_id, session_id, mfa_info.
    """
    import asyncio as _asyncio
    from mfa_phase import MfaError, enable_2fa

    settings = load_settings()
    sf_path = Path(session_file)
    if not sf_path.is_absolute():
        sf_path = settings.root_dir / sf_path
    if not sf_path.exists():
        typer.echo(f"Error: session file not found: {sf_path}", err=True)
        raise typer.Exit(1)

    try:
        sdata = json.loads(sf_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: invalid JSON in {sf_path}: {exc}", err=True)
        raise typer.Exit(1)

    access_token = sdata.get("access_token")
    if not access_token:
        typer.echo(f"Error: session file missing access_token", err=True)
        raise typer.Exit(1)

    user_agent = sdata.get("user_agent")
    if not user_agent:
        from user_agent_profile import WINDOWS_USER_AGENT
        user_agent = WINDOWS_USER_AGENT

    log = _emit_log(prefix="2fa")

    # --- Pre-init session_repo để dùng cho pending/on_enroll ---
    email = sdata.get("email")
    session_repo = None
    cli_engine = None
    if email:
        try:
            import atexit as _atexit_2fa_pre
            from db import get_engine, get_repos

            cli_engine = get_engine(db_path)
            _atexit_2fa_pre.register(cli_engine.close)
            _combo_repo, _job_repo, session_repo = get_repos(cli_engine)
        except Exception as exc:
            typer.echo(f"[warn] DB init failed (sẽ chạy mà không có pending recovery): {exc}", err=True)
            session_repo = None
            cli_engine = None

    # --- Load pending enrollment nếu có (recovery sau activate fail trước đó) ---
    pending = None
    if session_repo and email:
        try:
            pending = session_repo.get_mfa_pending(email)
            if pending:
                log(
                    f"[mfa] dùng pending enrollment factor_id="
                    f"{(pending.get('factor_id') or '')[:20]} (skip enroll)"
                )
        except Exception as exc:
            typer.echo(f"[warn] load pending failed: {exc}", err=True)

    # --- on_enroll callback: persist secret ngay sau enroll OK ---
    async def _on_enroll(state: dict) -> None:
        if session_repo and email:
            await _asyncio.to_thread(session_repo.set_mfa_pending, email, state)
            log(
                f"[mfa] pending persisted secret_len={len(state.get('secret') or '')}"
            )

    on_enroll_cb = _on_enroll if (session_repo and email) else None

    try:
        result = _asyncio.run(enable_2fa(
            access_token=access_token,
            user_agent=user_agent,
            proxy=proxy,
            activate=activate,
            pending_enrollment=pending,
            on_enroll=on_enroll_cb,
            log=log,
        ))
    except MfaError as exc:
        # Persist partial_state nếu có (best-effort, để retry kế thừa)
        if exc.partial_state and exc.partial_state.get("secret") and session_repo and email:
            try:
                session_repo.set_mfa_pending(email, {
                    "secret": exc.partial_state["secret"],
                    "factor_id": exc.partial_state.get("factor_id"),
                    "session_id": exc.partial_state.get("session_id"),
                    "status": "enrolled",
                })
                typer.echo(
                    "[info] partial enrollment đã persist vào DB — "
                    "lần chạy sau sẽ tái dùng (skip enroll).",
                    err=True,
                )
            except Exception as exc_p:
                typer.echo(f"[warn] persist partial state failed: {exc_p}", err=True)
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1)

    # --- SQLite persist: update two_factor column ---
    sqlite_ok = False
    if not email:
        if no_file_output:
            typer.echo("[error] session file thiếu 'email', không thể persist SQLite", err=True)
            raise typer.Exit(1)
        else:
            typer.echo("[warn] session file thiếu 'email', skip SQLite persist", err=True)
    elif session_repo is None:
        if no_file_output:
            typer.echo("[error] SQLite repo không khởi tạo được", err=True)
            raise typer.Exit(1)
        else:
            typer.echo("[warn] SQLite repo không khởi tạo được, skip persist", err=True)
    else:
        try:
            session_repo.update_2fa(email, result)
            # Clear pending sau khi 2fa đã commit thành công
            try:
                session_repo.clear_mfa_pending(email)
            except Exception as exc_clear:
                typer.echo(f"[warn] clear_mfa_pending failed (non-fatal): {exc_clear}", err=True)
            sqlite_ok = True
        except Exception as exc:
            if no_file_output:
                typer.echo(f"[error] SQLite persist failed: {exc}", err=True)
                raise typer.Exit(1)
            else:
                typer.echo(f"[warn] SQLite persist failed: {exc}", err=True)

    # --- File output (skip nếu --no-file-output) ---
    if not no_file_output:
        out_data = {
            "email": email,
            "user_id": sdata.get("user_id"),
            "account_id": sdata.get("account_id"),
            "two_factor": result,
        }

        if output is None:
            output = sf_path.with_suffix(".2fa.json")
        else:
            output = Path(output)
            if not output.is_absolute():
                output = settings.root_dir / output

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(out_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Summary output ---
    summary: dict = {
        "email": email,
        "secret": result["secret"],
        "first_code": result["first_code"],
        "activated": result["activated"],
        "provisioning_uri": result["provisioning_uri"],
        "sqlite_persisted": sqlite_ok,
    }
    if not no_file_output:
        summary["output"] = str(output)

    typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))


@app.command("migrate")
def migrate_cmd(
    state_dir: Path = typer.Option(
        "runtime/outlook_state",
        "--state-dir",
        help="Thư mục chứa outlook state JSON files.",
    ),
    sessions_dir: Path = typer.Option(
        "runtime/sessions",
        "--sessions-dir",
        help="Thư mục chứa session result JSON files.",
    ),
    db_path: str = typer.Option(
        "runtime/data.db",
        "--db-path",
        help="Đường dẫn tới SQLite database file.",
    ),
) -> None:
    """Migrate JSON state files (outlook_state + sessions) sang SQLite database."""
    import atexit as _atexit_migrate
    from db import get_engine, get_repos
    from db.migrate import MigrationTool

    engine = get_engine(db_path)
    _atexit_migrate.register(engine.close)
    combo_repo, _job_repo, session_repo = get_repos(engine)
    tool = MigrationTool(engine, combo_repo, session_repo)

    # Migrate outlook state
    outlook_summary = tool.migrate_outlook_state(Path(state_dir))
    # Migrate sessions
    sessions_summary = tool.migrate_sessions(Path(sessions_dir))

    # Print summary per entity type
    for summary in (outlook_summary, sessions_summary):
        typer.echo(
            f"[migrate] {summary.entity_type}: "
            f"total={summary.total_files} "
            f"inserted={summary.inserted} "
            f"skipped_duplicate={summary.skipped_duplicate} "
            f"skipped_error={summary.skipped_error}"
        )


@app.command("import-pool")
def import_pool_cmd(
    pool_file: Path = typer.Argument(..., help="Path tới pool file (format: email|password|refresh_token|client_id)."),
    db_path: str = typer.Option(
        "runtime/data.db",
        "--db-path",
        help="Đường dẫn tới SQLite database file.",
    ),
) -> None:
    """Import pool file vào SQLite database (upsert outlook_combos)."""
    pool_path = Path(pool_file)
    if not pool_path.exists():
        typer.echo(f"Error: pool file not found: {pool_path}", err=True)
        raise typer.Exit(1)

    import atexit as _atexit_pool
    from db import get_engine, get_repos
    from db.migrate import MigrationTool

    engine = get_engine(db_path)
    _atexit_pool.register(engine.close)
    combo_repo, _job_repo, session_repo = get_repos(engine)
    tool = MigrationTool(engine, combo_repo, session_repo)

    summary = tool.import_pool_file(pool_path)

    typer.echo(
        f"[import-pool] total_lines={summary.total_lines} "
        f"inserted={summary.inserted} "
        f"updated={summary.updated} "
        f"skipped={summary.skipped}"
    )


@app.command("record")
def record_cmd(
    url: str = typer.Option(
        "https://chatgpt.com/",
        "--url",
        help="URL mở lúc bắt đầu record.",
    ),
    output_root: Path | None = typer.Option(
        None,
        "--output-root",
        help="Thư mục chứa artifact record. Default: runtime/research_logs",
    ),
    email: str | None = typer.Option(
        None,
        "--email",
        help="Mailbox để dùng lệnh otp trong recorder (tuỳ chọn).",
    ),
    secret: str | None = typer.Option(
        None,
        "--secret",
        help="Secret đi kèm --email để fetch OTP trong recorder (tuỳ chọn).",
    ),
    otp_api_url: str = typer.Option(
        "https://cf-work-get-otp.n5pskgzs9g.workers.dev/api/get-code",
        "--otp-api-url",
        help="OTP API dùng khi gõ lệnh 'otp' trong recorder.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate wiring + tạo artifact skeleton, không mở browser.",
    ),
    headless: bool = typer.Option(
        False,
        "--headless/--headed",
        help="Chạy headless hay headed. Default: headed.",
    ),
    browser: str = typer.Option(
        "camoufox",
        "--browser",
        help="Browser engine: camoufox, chrome hoặc chromium.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help=(
            "Proxy URL để khớp môi trường thực (vd http://user:pass@host:port "
            "hoặc socks5://user:pass@host:port). Bắt buộc khi muốn reproduce "
            "đúng flow login dùng proxy ở runtime."
        ),
    ),
) -> None:
    """Record full DOM actions + HAR cho 1 web flow manual."""
    from web_recorder import (
        WebRecorderOptions,
        run_web_recording,
        validate_web_recorder_options,
    )

    settings = load_settings()
    resolved_output_root = output_root or (settings.runtime_dir / "research_logs")
    if not resolved_output_root.is_absolute():
        resolved_output_root = settings.root_dir / resolved_output_root

    options = WebRecorderOptions(
        url=url,
        output_root=resolved_output_root,
        email=email,
        secret=secret,
        otp_api_url=otp_api_url,
        dry_run=dry_run,
        headless=headless,
        browser=browser,
        proxy=proxy,
    )

    try:
        validate_web_recorder_options(options)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc

    rc = asyncio.run(run_web_recording(options))
    if rc != 0:
        raise typer.Exit(rc)


# Workaround: Typer thu gọn invoke khi chỉ có 1 command. Đăng ký một no-op
# command thứ hai để giữ form `python -m gpt_signup_hybrid signup ...`.
@app.command("version", hidden=True)
def _version_cmd() -> None:
    """Print package version (hidden helper)."""
    typer.echo("gpt_signup_hybrid 3.5.0")


@app.command("web")
def web_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8083, "--port", help="Bind port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload (dev mode)."),
    unsafe_expose_network: bool = typer.Option(
        False,
        "--unsafe-expose-network",
        help="Cho phép bind non-loopback host (LAN/0.0.0.0). Yêu cầu vì web UI "
             "trả secret-bearing job state — phải ý thức rủi ro.",
    ),
    hide_reg: bool = typer.Option(
        False,
        "--hide-reg",
        help="Ẩn tab Reg (giữ Get Session/UPI QR/Settings). Dùng khi giao máy "
             "cho người khác mà không cho chạy Reg. Lưu ý: chỉ ẩn UI — API tab "
             "Reg vẫn truy cập được nếu có token.",
    ),
    no_auth: bool = typer.Option(
        False,
        "--no-auth",
        help="TẮT token auth cho /api/* (INSECURE). Bất kỳ ai reach được server "
             "đều xem được credentials + điều khiển job. Chỉ dùng khi bạn hiểu "
             "rõ rủi ro (vd LAN tin cậy). KHÔNG nên bật khi public ra internet.",
    ),
) -> None:
    """Start web UI server tại http://<host>:<port>/.

    Web UI: textarea paste combo, list jobs, log panel, success/error output,
    mode single (1 job) / multi (max 3 song song).
    """
    import logging
    import os
    import signal
    import sys
    import uvicorn


    # ── Bind safety: chặn non-loopback nếu chưa opt-in ──
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not is_loopback and not unsafe_expose_network:
        typer.echo(
            f"[web] refuse bind to non-loopback host {host!r}.\n"
            f"      Web UI exposes credentials and job control without internet auth.\n"
            f"      Re-run với --unsafe-expose-network nếu bạn thật sự muốn.",
            err=True,
        )
        raise typer.Exit(2)

    # Suppress ALL uvicorn/asyncio noise
    logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

    # Set loopback bind mode cho server trước khi start
    from web.server import set_loopback_bind
    set_loopback_bind(is_loopback)

    # Bật chế độ ẩn tab Reg (launch flag, không persist vào Settings Store)
    from web.server import set_hide_reg
    set_hide_reg(hide_reg)

    # Tắt token auth (insecure, opt-in). Truyền qua env trong set_disable_auth.
    from web.server import set_disable_auth
    set_disable_auth(no_auth)

    # Truyền local endpoint cho Cloudflare Tunnel manager — manager sẽ trỏ
    # tunnel về 127.0.0.1:<port> ngay cả khi uvicorn bind LAN.
    from web.cloudflare_tunnel import get_cloudflare_tunnel
    get_cloudflare_tunnel().set_local_endpoint(host, port)

    # Print token cho user (nhất là non-loopback cần nhập thủ công)
    from web.auth import get_token as _get_web_token
    _token = _get_web_token()

    typer.echo(f"[web] starting at http://{host}:{port}/")
    if hide_reg:
        typer.echo("[web] HIDE-REG mode: ẩn tab Reg.")
    if no_auth:
        typer.echo(
            "[web] ⚠️  NO-AUTH mode: token auth ĐÃ TẮT — mọi client reach được "
            "server đều xem được credentials + điều khiển job. KHÔNG public ra "
            "internet ở chế độ này trừ khi bạn chấp nhận rủi ro."
        )
    if not is_loopback:
        typer.echo(
            f"[web] WARNING: bind {host!r} — UI reachable từ LAN."
        )
        typer.echo(f"[web] AUTH TOKEN (cần truyền qua URL ?token=...): {_token}")
    typer.echo(f"[web] Ctrl+C to stop.\n")

    # Monkey-patch: khi nhận SIGINT, suppress stderr rồi exit clean
    _original_stderr = sys.stderr

    def _quiet_shutdown(signum, frame):
        sys.stderr = open(os.devnull, "w")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _quiet_shutdown)

    try:
        uvicorn.run(
            "gpt_signup_hybrid.web.server:app",
            host=host,
            port=port,
            reload=reload,
            log_level="critical",
            timeout_graceful_shutdown=1,
        )
    except SystemExit:
        pass
    except Exception as exc:
        sys.stderr = _original_stderr
        typer.echo(f"[web] fatal error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        sys.stderr = _original_stderr
    typer.echo("\n[web] stopped.")


@app.command("signup")
def signup_cmd(
    email: str | None = typer.Option(
        None, "--email",
        help="Email đăng ký. Auto-derive từ --outlook-combo nếu không truyền.",
    ),
    name: str = typer.Option("ChatGPT User", "--name", help="Tên hiển thị."),
    birthdate: str = typer.Option("2000-01-01", "--birthdate", help="YYYY-MM-DD, tuổi >= 13."),
    reg_mode: str = typer.Option(
        "browser", "--reg-mode",
        help="Registration mode: 'browser' (Camoufox UI) hoặc 'hybrid' (curl_cffi Firefox + Camoufox sentinel oracle).",
    ),
    source_email: str | None = typer.Option(
        None, "--smail",
        help="Mailbox poll OTP (nếu khác email form).",
    ),
    # Provider selection
    mail_provider: str | None = typer.Option(
        None, "--mail-provider",
        help=(
            "'worker' | 'outlook' | 'icloud_v3'. Auto-detect: "
            "icloud_v3 nếu có --icloud-v3, "
            "outlook nếu có --outlook-combo, ngược lại worker."
        ),
    ),
    # iCloud v3 provider opts (Worker v2, URL gắn cứng per-mailbox)
    icloud_v3: str | None = typer.Option(
        None, "--icloud-v3",
        help=(
            "[icloud_v3] Cặp `email|api_url` (Worker v2 readmail URL). "
            "Auto-derive --email từ phần trước dấu `|`."
        ),
    ),
    # Worker provider opts
    logs_url: str = typer.Option(
        "https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs",
        "--logs-url",
        help="[worker] Worker logs URL.",
    ),
    api_key: str = typer.Option("12345678@", "--api-key", help="[worker] Bearer cho Worker."),
    insecure_tls: bool = typer.Option(
        False,
        "--insecure-tls/--secure-tls",
        help="[worker] Bỏ verify TLS (debug only). Default = secure.",
    ),
    # Outlook provider opts
    outlook_combo: str | None = typer.Option(
        None, "--outlook-combo",
        help="[outlook] Combo `email|password|refresh_token|client_id`.",
    ),
    outlook_combo_file: Path | None = typer.Option(
        None, "--outlook-combo-file",
        help="[outlook] File chứa combo (1 dòng), tránh leak combo qua shell history.",
    ),
    outlook_pool: Path | None = typer.Option(
        None, "--outlook-pool",
        help="[outlook] File pool nhiều combo (mỗi dòng 1 combo). Tự pick combo còn khả dụng.",
    ),
    # Browser opts
    headless: bool = typer.Option(False, "--headless/--headed"),
    off_font: bool = typer.Option(False, "--off-font", help="Tắt camoufox font randomization."),
    profile_template: bool = typer.Option(
        False, "--profile-template/--fresh-profile",
        help="Clone profile template (cookies cũ). Mặc định FALSE — fresh "
             "profile mỗi reg để tránh CF/Sentinel cluster ban (anti-ban "
             "hardening 2026-06-25, journal 260625-1224).",
    ),
    proxy: str | None = typer.Option(None, "--proxy", help="HTTP/HTTPS proxy."),
    browser_tls_insecure: bool = typer.Option(
        False,
        "--browser-tls-insecure/--browser-tls-secure",
        help="Bỏ TLS verify cho browser context (debug only). Default = secure.",
    ),
    # Timing
    otp_timeout: float = typer.Option(180.0, "--otp-timeout", min=10),
    otp_interval: float = typer.Option(4.0, "--otp-interval", min=0.5),
    sentinel_timeout: float = typer.Option(30.0, "--sentinel-timeout", min=5),
    har_capture: bool = typer.Option(False, "--har/--no-har", help="Bật HAR capture Phase 1 cho debug."),
    har_validate: bool = typer.Option(
        False, "--har-validate/--no-har-validate",
        help=(
            "Sau khi reg xong, tự chạy `test/check_har_alignment.py` để so HAR "
            "runtime với golden record (anti-ban Phase 5). Yêu cầu --har. "
            "Override Settings Store ``reg.har_validate``."
        ),
    ),
    persona: str = typer.Option(
        "firefox_mac", "--persona",
        help=(
            "Browser persona cho REG: 'firefox_mac' (default — Camoufox khớp Firefox 135 Mac) "
            "hoặc 'chrome_win' (Chrome 145 Windows — chỉ dùng khi engine=chromium). "
            "Override Settings Store ``reg.persona``."
        ),
    ),
    mfa_inline: bool = typer.Option(
        True, "--mfa/--no-mfa",
        help=(
            "Enroll + activate 2FA NGAY trong context vừa tạo account (CF-clean) "
            "và xuất email|password|secret vào `runtime/sessions/accounts.txt`. "
            "Default True. Tắt → bỏ qua 2FA, có thể chạy `enable-2fa` riêng sau."
        ),
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Lưu SignupResult ra JSON file."),
    # SQLite persistence opts
    no_file_output: bool = typer.Option(
        False, "--no-file-output",
        help="Skip JSON file creation, chỉ persist vào SQLite.",
    ),
    db_path: str = typer.Option(
        "runtime/data.db",
        "--db-path",
        help="Đường dẫn tới SQLite database file.",
    ),
) -> None:
    """Chạy 1 lần signup hybrid."""
    settings = load_settings()

    if reg_mode not in ("browser", "hybrid"):
        typer.echo(
            f"Error: --reg-mode phải là 'browser' hoặc 'hybrid', got {reg_mode!r}.",
            err=True,
        )
        raise typer.Exit(2)

    # Resolve combo từ file nếu cần
    if outlook_combo_file is not None:
        combo_path = Path(outlook_combo_file)
        if not combo_path.is_absolute():
            combo_path = settings.root_dir / combo_path
        if not combo_path.exists():
            typer.echo(f"Error: combo file not found: {combo_path}", err=True)
            raise typer.Exit(1)
        outlook_combo = combo_path.read_text(encoding="utf-8").strip().splitlines()[0].strip()

    # Sentinel — pool block có thể init DB sớm, reuse sau
    _pool_engine = None
    _pool_combo_repo = None

    # Resolve từ pool — tự pick combo còn khả dụng
    if outlook_pool is not None:
        pool_path = Path(outlook_pool)
        if not pool_path.is_absolute():
            pool_path = settings.root_dir / pool_path
        from outlook_pool import (
            OutlookPoolError,
            parse_pool_file,
            pick_first_available,
            status_summary,
        )

        try:
            pool = parse_pool_file(pool_path)
        except OutlookPoolError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)

        state_dir = settings.runtime_dir / "outlook_state"

        # Init DB sớm để pick_first_available dùng SQLite làm source of truth
        _pool_combo_repo = None
        try:
            import atexit as _pool_atexit
            from db import get_engine, get_repos

            _pool_engine = get_engine(db_path)
            _pool_atexit.register(_pool_engine.close)
            _pool_combo_repo, _, _ = get_repos(_pool_engine)

            # Ensure pool file rows exist before pick. Runtime path must preserve
            # refresh_token already rotated in SQLite; explicit import-pool still
            # uses upsert for intentional sync.
            for combo in pool:
                _pool_combo_repo.ensure_exists({
                    "email": combo.email,
                    "password": combo.password,
                    "refresh_token": combo.refresh_token,
                    "client_id": combo.client_id,
                })
        except Exception as exc:
            typer.echo(f"[warning] SQLite init (pool pick): {exc} — fallback JSON state", err=True)
            _pool_combo_repo = None

        summary = status_summary(pool, state_dir=state_dir, combo_repo=_pool_combo_repo)
        typer.echo(
            f"[pool] {pool_path}: total={summary['total']} "
            f"used={summary['used_for_signup']} "
            f"available={summary['available']} "
            f"terminal_error={summary['terminal_error']}"
        )

        try:
            picked = pick_first_available(
                pool, state_dir=state_dir, log=lambda m: typer.echo(m),
                combo_repo=_pool_combo_repo,
            )
        except OutlookPoolError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)

        outlook_combo = "|".join((
            picked.email, picked.password, picked.refresh_token, picked.client_id,
        ))

    # Parse --icloud-v3 (email|url) trước khi auto-detect provider
    icloud_v3_email: str | None = None
    icloud_v3_url: str | None = None
    if icloud_v3:
        from mail_providers import IcloudV3ParseError, IcloudV3Provider
        try:
            icloud_v3_email, icloud_v3_url = IcloudV3Provider.parse_line(icloud_v3)
        except IcloudV3ParseError as exc:
            typer.echo(f"Error: --icloud-v3 invalid: {exc}", err=True)
            raise typer.Exit(2)

    # Auto-detect provider — order khớp với help text
    resolved_provider = mail_provider
    if resolved_provider is None:
        if icloud_v3_url:
            resolved_provider = "icloud_v3"
        elif outlook_combo:
            resolved_provider = "outlook"
        else:
            resolved_provider = "worker"

    # Validate provider known (regex pattern enforces this at SignupRequest level
    # but giúp CLI fail-fast với message dễ hiểu hơn ValidationError).
    if resolved_provider not in ("icloud_v3", "worker", "outlook", "dongvanfb", "gmail_advanced", "china_icloud"):
        typer.echo(
            f"Error: --mail-provider {resolved_provider!r} không hỗ trợ. "
            f"Chọn: icloud_v3 | worker | outlook | dongvanfb | gmail_advanced | china_icloud.",
            err=True,
        )
        raise typer.Exit(2)

    # Auto-derive email từ provider input nếu không truyền --email
    if resolved_provider == "outlook" and outlook_combo and not email:
        first_part = outlook_combo.split("|", 1)[0].strip()
        if "@" in first_part:
            email = first_part
            typer.echo(f"[cli] auto email={email} (từ outlook combo)")
    if resolved_provider == "icloud_v3" and icloud_v3_email and not email:
        email = icloud_v3_email
        typer.echo(f"[cli] auto email={email} (từ --icloud-v3)")

    # Validate provider yêu cầu input bắt buộc
    if resolved_provider == "icloud_v3" and not icloud_v3_url:
        typer.echo(
            "Error: --mail-provider=icloud_v3 yêu cầu --icloud-v3 'email|api_url'.",
            err=True,
        )
        raise typer.Exit(2)

    if not email:
        typer.echo(
            "Error: --email is required (hoặc --outlook-combo / --outlook-pool).",
            err=True,
        )
        raise typer.Exit(1)

    request = SignupRequest(
        email=email,
        name=name,
        birthdate=birthdate,
        reg_mode=reg_mode,
        source_email=source_email,
        mail_provider=resolved_provider,
        email_logs_url=logs_url,
        email_api_key=api_key,
        email_insecure_tls=insecure_tls,
        outlook_combo=outlook_combo,
        icloud_v3_url=icloud_v3_url,
        headless=headless,
        off_font=off_font,
        profile_template=profile_template,
        proxy=proxy,
        tls_insecure=browser_tls_insecure or env_insecure_tls(),
        otp_timeout_seconds=otp_timeout,
        otp_poll_interval_seconds=otp_interval,
        sentinel_cookie_timeout_seconds=sentinel_timeout,
        har_capture=har_capture,
        persona=persona,
        # MFA inline: hybrid + browser đều enroll 2FA trong context
        # vừa pass CF (page sống / curl session sống) → CF-clean, không bị 403.
        # Default True. Tắt qua `--no-mfa` nếu muốn enroll riêng sau qua
        # command `enable-2fa`.
        mfa_inline=mfa_inline,
    )

    log = _emit_log()
    # Initialize DB early for combo_repo wiring into provider
    # Reuse engine nếu đã init ở pool pick flow (tránh mở connection pool thứ 2)
    _signup_combo_repo = None
    _signup_session_repo = None
    _signup_engine = None  # type: ignore[assignment]
    if _pool_engine is not None and _pool_combo_repo is not None:
        try:
            from db import get_repos
            _signup_engine = _pool_engine
            _signup_combo_repo, _signup_job_repo, _signup_session_repo = get_repos(_pool_engine)
        except Exception:
            _signup_engine = None
            pass  # fallback: init mới bên dưới
    if _signup_combo_repo is None:
        try:
            import atexit as _atexit
            from db import get_engine, get_repos

            _signup_engine = get_engine(db_path)
            _atexit.register(_signup_engine.close)
            _signup_combo_repo, _signup_job_repo, _signup_session_repo = get_repos(_signup_engine)
        except Exception as exc:
            if no_file_output:
                typer.echo(f"[error] SQLite init failed: {exc}", err=True)
                sys.exit(1)
            else:
                typer.echo(f"[warning] SQLite init failed: {exc} — continuing without DB persistence", err=True)

    # Timestamp dùng chung cho filename + DB created_at (đảm bảo dedupe nhất quán)
    _signup_ts = datetime.now()
    _signup_created_at = _signup_ts.strftime("%Y-%m-%dT%H:%M:%S")
    _signup_filename_ts = _signup_ts.strftime("%Y%m%d-%H%M%S")

    # Ensure combo row tồn tại trong DB TRƯỚC khi chạy signup — đảm bảo row có cho
    # mark_success/mark_failure, và nếu provider rotate refresh token trong lúc chạy,
    # update_refresh_token() sẽ ghi token mới. Dùng ensure_exists để PRESERVE token
    # đã rotate trước đó (không overwrite bằng token cũ từ CLI arg).
    if _signup_combo_repo is not None and resolved_provider == "outlook" and outlook_combo:
        try:
            from mail_providers import OutlookCombo as _OC_pre
            _parsed_pre = _OC_pre.parse(outlook_combo)
            _signup_combo_repo.ensure_exists({
                "email": _parsed_pre.email,
                "password": _parsed_pre.password,
                "refresh_token": _parsed_pre.refresh_token,
                "client_id": _parsed_pre.client_id,
            })
        except Exception as exc:
            if no_file_output:
                typer.echo(f"[error] combo ensure_exists failed: {exc}", err=True)
                sys.exit(1)
            else:
                typer.echo(f"[warning] combo ensure_exists failed: {exc} — continuing without DB combo tracking", err=True)
                _signup_combo_repo = None  # disable combo repo for this run

    # Run signup. SQLite write trong provider (refresh-token rotation) có thể raise
    # DatabaseError lên đây — Req 9.5 yêu cầu warn + tiếp tục JSON output khi
    # not no_file_output, exit non-zero khi no_file_output.
    from db.engine import DatabaseError as _DBError
    sqlite_persist_ok = True
    try:
        result: SignupResult = asyncio.run(
            run_signup(request, log=log, combo_repo=_signup_combo_repo)
        )
    except _DBError as exc:
        sqlite_persist_ok = False
        if no_file_output:
            typer.echo(
                f"[error] SQLite persist failed during signup: {exc}",
                err=True,
            )
            sys.exit(1)
        typer.echo(
            f"[warning] SQLite persist failed during signup: {exc} — continuing with file output",
            err=True,
        )
        # Disable combo_repo cho phần persist phía sau (DB đang lỗi).
        _signup_combo_repo = None
        result = SignupResult(
            success=False,
            email=request.email,
            error=f"{type(exc).__name__}: {exc}",
        )

    # --- SQLite persistence ---
    # Skip atomic persist khi DB đã fail trong run_signup (provider refresh-token
    # rotation đã raise DatabaseError ở trên) — tránh warn 2 lần và lỗi tiếp tầng.
    if sqlite_persist_ok:
        try:
            combo_repo = _signup_combo_repo
            session_repo = _signup_session_repo if _signup_combo_repo else None

            if combo_repo is None or session_repo is None:
                raise RuntimeError("SQLite repos not initialized")

            # Atomic: session insert + combo mark trong single outer transaction.
            # Nhờ reentrant engine, repo methods chạy bên trong outer tx
            # mà không deadlock — chỉ outer scope COMMIT/ROLLBACK.
            if result.success:
                with _signup_engine.get_connection():
                    session_repo.create({
                        "email": result.email,
                        "password": result.password,
                        "name": result.name,
                        "age": result.age,
                        "user_id": result.user_id,
                        "account_id": result.account_id,
                        "session_token": result.session_token,
                        "access_token": result.access_token,
                        "cookies": result.cookies,
                        "phase1_seconds": result.phase1_seconds,
                        "phase2_seconds": result.phase2_seconds,
                        "otp_seconds": result.otp_seconds,
                        "created_at": _signup_created_at,
                    })
                    if resolved_provider == "outlook" and outlook_combo:
                        combo_repo.mark_success(email)
                typer.echo(f"[db] session result + combo persisted atomically for {email}")
            elif resolved_provider == "outlook" and outlook_combo:
                combo_repo.mark_failure(email, result.error or "unknown")
                typer.echo(f"[db] recorded failure for {email}: {(result.error or '')[:80]}")
        except Exception as exc:
            sqlite_persist_ok = False
            if no_file_output:
                typer.echo(f"[error] SQLite persist failed: {exc}", err=True)
                sys.exit(1)
            else:
                typer.echo(f"[warning] SQLite persist failed: {exc} — continuing with file output", err=True)

    # --- Fallback: JSON file pool state (khi SQLite persist thất bại) ---
    if not sqlite_persist_ok and resolved_provider == "outlook" and outlook_combo:
        from outlook_pool import mark_signup_failure, mark_signup_success
        state_dir = settings.runtime_dir / "outlook_state"
        if result.success:
            mark_signup_success(state_dir=state_dir, email=email)
            typer.echo(f"[pool] marked {email} as used_for_signup=true (file fallback)")
        else:
            mark_signup_failure(state_dir=state_dir, email=email, error=result.error or "unknown")
            typer.echo(f"[pool] recorded failure for {email} (file fallback): {(result.error or '')[:80]}")

    # --- JSON file output ---
    if not no_file_output:
        if output is None:
            output = runtime_session_dir(settings) / f"signup-{_signup_filename_ts}-{email.replace('@','_at_')}.json"
        else:
            output = Path(output)
            if not output.is_absolute():
                output = settings.root_dir / output
        output.parent.mkdir(parents=True, exist_ok=True)

        payload = result.model_dump()
        output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Print summary ---
    payload = result.model_dump()
    summary = {k: v for k, v in payload.items() if k not in ("cookies", "session_token", "access_token")}
    summary["session_token_len"] = len(result.session_token or "")
    summary["access_token_len"] = len(result.access_token or "")
    summary["cookies_count"] = len(result.cookies or [])
    # 2FA summary — KHÔNG leak secret/factor_id vào stdout (chỉ flag + len).
    tf = result.two_factor or {}
    summary["two_factor_activated"] = bool(tf.get("activated"))
    summary["two_factor_secret_len"] = len(tf.get("secret") or "")
    if not no_file_output:
        summary["output"] = str(output)

    typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))

    # ── Append email|password|secret vào accounts.txt (combo file tổng hợp) ──
    # Đồng nhất với `web/manager._append_account_log` (web UI dùng). Reg manual
    # qua CLI cũng nên có 1 nguồn duy nhất để autoreg/import tools đọc lại.
    # Chỉ append khi 2FA actually activated — `secret` rỗng coi như chưa enable.
    if (
        result.success
        and result.two_factor
        and result.two_factor.get("activated")
        and result.two_factor.get("secret")
        and result.password
    ):
        accounts_path = settings.runtime_dir / "sessions" / "accounts.txt"
        accounts_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{result.email}|{result.password}|{result.two_factor['secret']}\n"
        with accounts_path.open("a", encoding="utf-8") as f:
            f.write(line)
        typer.echo(f"[combo] appended → {accounts_path}")
    elif result.success and result.two_factor_partial:
        # Enroll OK + activate fail → log để user retry activate-only.
        typer.echo(
            f"[combo] 2FA partial — secret persisted ở DB mfa_pending, "
            f"chạy `enable-2fa --session-file <...>` để retry activate"
        )
    elif result.success and mfa_inline:
        typer.echo(
            "[combo] 2FA chưa active — accounts.txt skip "
            f"(two_factor={bool(result.two_factor)} partial={bool(result.two_factor_partial)})"
        )

    # ── HAR alignment post-validation (anti-ban Phase 5 Task 5.3) ──
    # Chỉ run nếu --har-validate + --har enabled + reg success.
    if har_validate and har_capture and result.success:
        _run_har_alignment_validate(settings, log)

    if not result.success:
        sys.exit(1)


def _run_har_alignment_validate(settings, log) -> None:
    """Chạy test/check_har_alignment.py trên HAR runtime mới nhất.

    Anti-ban Phase 5 Task 5.3: post-reg validate runtime HAR vs golden record
    để bắt regression sentinel/cookie/endpoint sequence sớm. KHÔNG fail reg
    (chỉ log warning cho user xem).
    """
    import subprocess as _sp

    har_dir = settings.runtime_dir / "har_hybrid"
    if not har_dir.exists():
        log("[har-validate] no HAR dir — skip")
        return
    har_files = sorted(har_dir.glob("hybrid-*.har"), key=lambda p: p.stat().st_mtime)
    if not har_files:
        log("[har-validate] no HAR files in dir — skip")
        return
    latest = har_files[-1]
    log(f"[har-validate] running on latest HAR: {latest.name}")
    script = settings.root_dir / "test" / "check_har_alignment.py"
    if not script.exists():
        log(f"[har-validate] script missing: {script} — skip")
        return
    try:
        proc = _sp.run(
            [str(settings.root_dir / ".venv" / "bin" / "python3"),
             str(script), str(latest)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            log("[har-validate] PASS — runtime khớp golden")
        else:
            log("[har-validate] FAIL — review output:")
            for line in (proc.stdout + proc.stderr).splitlines():
                log(f"  {line}")
    except Exception as exc:
        log(f"[har-validate] error (non-fatal): {exc}")


if __name__ == "__main__":
    app()
