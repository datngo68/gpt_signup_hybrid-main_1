"""CLI cho icloud_hme — phase MVP (R3, R4, R5, R6, R7, R8, R12, R1) +
phase sau MVP (R9, R13) + Runner integration (icloud-runner-loop R8).

Refs:
    requirements.md (toàn bộ MVP scope + R9 + R13)
    design.md §Components / 1. CLI / §11 CLI table
    tasks.md task 20 + task 28
    .kiro/specs/icloud-runner-loop/ (task 4.1: generate qua HmeRunner)

Usage MVP:
    python -m gpt_signup_hybrid.icloud_hme bootstrap --apple-id you@icloud.com
    python -m gpt_signup_hybrid.icloud_hme generate          # infinite loop, Ctrl+C để dừng
    python -m gpt_signup_hybrid.icloud_hme generate -n 10    # tối đa 10 email/cycle
    python -m gpt_signup_hybrid.icloud_hme check [--apple-id] [--all]
    python -m gpt_signup_hybrid.icloud_hme status
    python -m gpt_signup_hybrid.icloud_hme recording start --apple-id --scenario
    python -m gpt_signup_hybrid.icloud_hme recording stop --session-id
    python -m gpt_signup_hybrid.icloud_hme audit list [--apple-id] [--event-type]
    python -m gpt_signup_hybrid.icloud_hme audit cleanup [--days]
    python -m gpt_signup_hybrid.icloud_hme profile delete --apple-id
    python -m gpt_signup_hybrid.icloud_hme reconcile --apple-id

Usage phase F (R9, R13 — task 28):
    icloud_hme email deactivate --email <EMAIL> [--dry-run]
    icloud_hme email reactivate --email <EMAIL> [--dry-run]
    icloud_hme email delete --email <EMAIL> [--dry-run]
    icloud_hme email update-meta --email <EMAIL> [--label X] [--note Y] [--dry-run]
    icloud_hme email mark-used --email <EMAIL> --used-for <UseId>
    icloud_hme email list-sync --apple-id <ID>
    icloud_hme email list [--status X] [--apple-id Y] [--label Z] [--limit N]
    icloud_hme email export --format csv|json [--apple-id X] [--output PATH]

Exit code (R3 / design §Failure semantics):
    - 0: success (created==requested) hoặc partial (0 < created < requested).
    - 1: created == 0 hoặc fatal error.
    - 130: user interrupted (SIGINT).
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from config import Settings, load_settings
from db import get_engine
from db.repositories import (
    AuditLogRepository,
    IcloudPoolRepository,
)
from .bootstrap import BootstrapError, bootstrap as bootstrap_v2
from .checker import ProfileChecker
from .exceptions import IcloudPoolError
from .generator import HmeGenerator
from .manager import HmeManager
from .pool import IcloudPoolManager
from .recorder import Recorder, RecorderError
from .runner import HmeRunner


app = typer.Typer(no_args_is_help=True, add_completion=False)
recording_app = typer.Typer(no_args_is_help=True, add_completion=False)
audit_app = typer.Typer(no_args_is_help=True, add_completion=False)
profile_app = typer.Typer(no_args_is_help=True, add_completion=False)
email_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(recording_app, name="recording", help="Record Camoufox session for discovery.")
app.add_typer(audit_app, name="audit", help="Audit log query / cleanup.")
app.add_typer(profile_app, name="profile", help="Profile lifecycle (delete, etc).")
app.add_typer(email_app, name="email", help="HME email lifecycle (deactivate / reactivate / delete / mark-used).")


def _emit_log(prefix: str = "icloud"):
    def log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        typer.echo(f"[{ts}][{prefix}] {msg}", err=True)

    return log


def _init_repos(db_path: str):
    """Init engine + repository tuple. Caller register atexit cleanup."""
    engine = get_engine(db_path)
    atexit.register(engine.close)
    pool_repo = IcloudPoolRepository(engine)
    audit_repo = AuditLogRepository(engine)
    return engine, pool_repo, audit_repo


def _emit_json(payload: Any) -> None:
    """In JSON output ra stdout."""
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# =====================================================================
# Runner wiring (icloud-runner-loop, task 4.1)
# =====================================================================


async def _cli_log(level: str, message: str, payload: dict) -> None:
    """``log_callback`` cho HmeRunner khi chạy qua CLI.

    Format stderr: ``[HH:MM:SS][level] message`` (R10.3).
    Async signature theo R10.1: ``(level, message, payload) -> Awaitable[None]``.
    Payload không in để giữ dòng log gọn — caller có thể dump qua ``-v`` về
    sau nếu cần.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {message}", file=sys.stderr, flush=True)


def _build_runner_for_cli(
    *,
    settings: Settings,
    pool_repo: IcloudPoolRepository,
    audit_repo: AuditLogRepository,
    delay_range: tuple[float, float],
    retry_interval: int | None = None,
) -> HmeRunner:
    """Khởi tạo Runner kèm đầy đủ service layer cho CLI.

    Runner cần generator + checker + hme_manager + pool_manager kể cả khi
    action chỉ là "generate" — constructor của Runner luôn require đủ deps
    để dispatch về action khác sau này (R6).

    Args:
        settings: ``Settings.from_env(os.environ)`` đã validate.
        pool_repo: ``IcloudPoolRepository`` (đã wire engine + cleanup).
        audit_repo: ``AuditLogRepository``.
        delay_range: ``(delay_min, delay_max)`` truyền vào Generator.
        retry_interval: override ``Settings.icloud_retry_interval`` nếu set
            (None = lấy từ settings).
    """
    log_legacy = _emit_log("icloud")  # service layer (sync log) — không phải log_callback Runner
    pool_mgr = IcloudPoolManager(
        pool_repo,
        audit_repo,
        limited_ttl_hours=settings.icloud_limited_ttl_hours,
        quota_retry_minutes=settings.icloud_quota_retry_minutes,
        hme_quota_limit=settings.icloud_hme_quota_limit,
        log=log_legacy,
    )
    generator = HmeGenerator(
        pool_mgr,
        pool_repo,
        audit_repo,
        race_retry_max=settings.icloud_hme_race_retry_max,
        delay_range=delay_range,
        infinite_wait_max_sec=settings.icloud_infinite_wait_max_sec,
        hme_quota_limit=settings.icloud_hme_quota_limit,
        log=log_legacy,
    )
    checker = ProfileChecker(pool_mgr, pool_repo, audit_repo, log=log_legacy)
    hme_manager = HmeManager(pool_mgr, pool_repo, audit_repo, log=log_legacy)

    return HmeRunner(
        generator=generator,
        checker=checker,
        hme_manager=hme_manager,
        pool_manager=pool_mgr,
        settings=settings,
        log_callback=_cli_log,
        retry_interval=retry_interval,
    )


async def _run_runner_with_sigint(
    runner: HmeRunner, *, action: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Chạy ``runner.start(action, params)`` với SIGINT/SIGTERM handler.

    Đăng ký signal handler trên running loop để gọi ``runner.stop()`` thay vì
    để Python raise ``KeyboardInterrupt`` (R8.3). Handler chỉ set
    ``cancel_event`` — Runner tự thoát ở checkpoint kế tiếp và trả summary
    bình thường.
    """
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        runner.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            # Một số platform (Windows + non-main thread) không support
            # add_signal_handler — bỏ qua, fallback sẽ là default Python handler.
            pass

    return await runner.start(action=action, params=params)


def _run_with_runner_lock(
    runtime_dir,
    runner: HmeRunner,
    *,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Chạy ``_run_runner_with_sigint`` bên trong cross-process lock.

    Logic:
        1. Acquire ``RunnerLock(runtime_dir)`` — fail-fast nếu Web server
           hoặc CLI khác đang chạy runner (tránh race ở pool reserve).
        2. ``asyncio.run(_run_runner_with_sigint(...))`` — block tới khi
           runner kết thúc (SIGINT hoặc cycle done).
        3. Release lock trong ``finally`` (cũng được kernel auto-release
           khi process chết → an toàn với SIGKILL).

    Khi lock acquire fail → in error message ngắn ra stderr + raise
    ``typer.Exit(1)`` — KHÔNG fallback chạy runner song song (tuân
    project-rules: fail-fast, không default insecure).
    """
    from .runner_lock import RunnerLock, RunnerLockError

    lock = RunnerLock(runtime_dir)
    try:
        lock.acquire()
    except RunnerLockError as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from None

    try:
        return asyncio.run(
            _run_runner_with_sigint(runner, action=action, params=params)
        )
    finally:
        lock.release()


def _validate_retry_interval(value: int | None) -> None:
    """Fail-fast nếu user truyền ``--retry-interval`` < 10 (R8.5).

    Khớp ràng buộc của ``Settings.icloud_retry_interval`` (min 10) — tránh
    spam Apple HME endpoint. ``None`` = chấp nhận, để Runner lấy từ Settings.
    """
    if value is None:
        return
    if value < 10:
        typer.echo(
            f"[error] --retry-interval phải >= 10 giây (got {value})",
            err=True,
        )
        raise typer.Exit(2)


# =====================================================================
# bootstrap
# =====================================================================


@app.command("bootstrap")
def bootstrap_cmd(
    apple_id: str = typer.Option(..., "--apple-id", help="Apple ID email."),
    proxy: str | None = typer.Option(None, "--proxy", help="HTTP proxy URL."),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Bootstrap_Flow — login Apple ID + 2FA tay trong Camoufox headed (R12.2)."""
    settings = load_settings()
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("bootstrap")

    try:
        result = asyncio.run(
            bootstrap_v2(
                apple_id,
                runtime_dir=settings.runtime_dir,
                pool_repo=pool_repo,
                audit_repo=audit_repo,
                proxy=proxy,
                log=log,
            )
        )
    except BootstrapError as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.echo("[error] interrupted", err=True)
        raise typer.Exit(130)

    _emit_json(
        {
            "ok": True,
            "apple_id": result.apple_id,
            "profile_dir": str(result.profile_dir),
            "status": result.status,
            "matched_cookies": result.matched_cookies,
            "bootstrapped_at": result.bootstrapped_at.isoformat() + "Z",
        }
    )


# =====================================================================
# generate (bounded + infinite blocking mode)
# =====================================================================


@app.command("generate")
def generate_cmd(
    count_per_cycle: int | None = typer.Option(
        None,
        "--count-per-cycle",
        "-n",
        help="Số email tối đa MỖI cycle (None = drain tới khi pool exhausted).",
    ),
    retry_interval: int | None = typer.Option(
        None,
        "--retry-interval",
        help=(
            "Giây sleep giữa các cycle (>=10). Bỏ trống → lấy từ "
            "ICLOUD_RETRY_INTERVAL hoặc default 900."
        ),
    ),
    label: str | None = typer.Option(
        None, "--label", help="Label cho email. Default = strftime('%Y%m%d', UTC)."
    ),
    note: str | None = typer.Option(None, "--note"),
    delay_min: float = typer.Option(2.0, "--delay-min"),
    delay_max: float = typer.Option(5.0, "--delay-max"),
    proxy: str | None = typer.Option(None, "--proxy"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Generate HME email — infinite loop qua HmeRunner (R8.1, R8.4, R8.5, R8.6).

    Chạy vô hạn theo chu kỳ ``cycle → wait retry_interval → cycle``. Mỗi
    cycle tạo tối đa ``--count-per-cycle`` email (None = drain pool). Dừng
    bằng ``Ctrl+C`` (SIGINT) → CLI gọi ``runner.stop()`` → summary cuối
    session.

    Refs: icloud-runner-loop R8.1, R8.3, R8.4, R8.5, R8.6, R10.3.
    """
    _validate_retry_interval(retry_interval)
    settings = Settings.from_env(os.environ)
    _, pool_repo, audit_repo = _init_repos(db_path)

    runner = _build_runner_for_cli(
        settings=settings,
        pool_repo=pool_repo,
        audit_repo=audit_repo,
        delay_range=(delay_min, delay_max),
        retry_interval=retry_interval,
    )

    params: dict[str, Any] = {
        "count_per_cycle": count_per_cycle,
        "label": label,
        "note": note,
        "proxy": proxy,
    }

    # Cross-process single-instance lock (icloud_hme/runner_lock.py): chặn
    # đồng thời CLI + Web hoặc 2 CLI cùng chạy generate → tránh race ở
    # IcloudPoolManager.reserve() level. Lock release tự động khi process
    # chết (kernel-level fcntl) — không cần cleanup pidfile bằng tay.
    summary = _run_with_runner_lock(
        settings.runtime_dir,
        runner,
        action="generate",
        params=params,
    )
    _emit_json(summary)


# =====================================================================
# check
# =====================================================================


@app.command("check")
def check_cmd(
    apple_id: str | None = typer.Option(None, "--apple-id"),
    check_all: bool = typer.Option(False, "--all", help="Check toàn bộ profile."),
    auto_mark: bool = typer.Option(True, "--auto-mark/--no-auto-mark"),
    retry_interval: int | None = typer.Option(
        None,
        "--retry-interval",
        help=(
            "Giây sleep giữa các cycle (>=10). Chỉ áp dụng cho mode --all. "
            "Bỏ trống → lấy từ ICLOUD_RETRY_INTERVAL hoặc default 900."
        ),
    ),
    proxy: str | None = typer.Option(None, "--proxy"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Profile_Checker probe session validity (R4).

    Hai mode:

    * ``--all``: chạy infinite loop qua ``HmeRunner`` (R8.2). Mỗi cycle gọi
      ``ProfileChecker.check_all`` rồi sleep ``retry_interval`` cho tới khi
      ``Ctrl+C`` (SIGINT) → ``runner.stop()`` (R8.3) → in summary. Cờ
      ``--retry-interval`` (R8.5) override giá trị từ Settings.
    * ``--apple-id``: 1-shot single profile (R8.7) — dùng
      ``checker.check_one`` trực tiếp, không qua Runner.
    """
    if not apple_id and not check_all:
        typer.echo("[error] cần --apple-id hoặc --all", err=True)
        raise typer.Exit(2)

    _validate_retry_interval(retry_interval)
    _, pool_repo, audit_repo = _init_repos(db_path)

    if check_all:
        # Infinite loop qua Runner (icloud-runner-loop R8.2, R8.3, R8.5).
        settings = Settings.from_env(os.environ)
        runner = _build_runner_for_cli(
            settings=settings,
            pool_repo=pool_repo,
            audit_repo=audit_repo,
            delay_range=(2.0, 5.0),
            retry_interval=retry_interval,
        )
        params: dict[str, Any] = {
            "auto_mark": auto_mark,
            "proxy": proxy,
        }
        # Cross-process single-instance lock — same lý do với generate_cmd.
        summary = _run_with_runner_lock(
            settings.runtime_dir,
            runner,
            action="check_all",
            params=params,
        )
        _emit_json(summary)
        return

    # 1-shot single profile (R8.7): không qua Runner.
    log = _emit_log("check")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    checker = ProfileChecker(pool_mgr, pool_repo, audit_repo, log=log)

    try:
        result = asyncio.run(
            checker.check_one(apple_id, auto_mark=auto_mark, proxy=proxy)
        )
        _emit_json(
            {
                "apple_id": result.apple_id,
                "ok": result.ok,
                "status": result.status,
                "hme_count_remote": result.hme_count_remote,
                "hme_count_local": result.hme_count_local,
                "error": result.error,
            }
        )
    except KeyboardInterrupt:
        raise typer.Exit(130)


# =====================================================================
# status (R7)
# =====================================================================


@app.command("status")
def status_cmd(
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Pool status report (R7.1, R7.5)."""
    settings = load_settings()
    _, pool_repo, audit_repo = _init_repos(db_path)
    pool_mgr = IcloudPoolManager(
        pool_repo,
        audit_repo,
        limited_ttl_hours=settings.icloud_limited_ttl_hours,
        quota_retry_minutes=settings.icloud_quota_retry_minutes,
        hme_quota_limit=settings.icloud_hme_quota_limit,
        log=None,
    )
    report = pool_mgr.status_report()

    _emit_json(
        {
            "by_status": report.by_status,
            "profiles": [
                {
                    "apple_id": p.apple_id,
                    "status": p.status,
                    "hme_count": p.hme_count,
                    "quota_remaining": p.quota_remaining,
                    "limited_until": p.limited_until.isoformat() + "Z"
                    if p.limited_until
                    else None,
                    "quota_retry_until": p.quota_retry_until.isoformat() + "Z"
                    if p.quota_retry_until
                    else None,
                    "last_used_at": p.last_used_at.isoformat() + "Z"
                    if p.last_used_at
                    else None,
                    "last_error": p.last_error,
                }
                for p in report.profiles
            ],
            "emails_by_status": report.emails_by_status,
            "quota_soft_cap_per_account": report.quota_soft_cap_per_account,
            "total_quota_remaining": report.total_quota_remaining,
            "low_capacity": report.low_capacity,
            "quota_full_count": report.quota_full_count,
            "quota_full_profiles": report.quota_full_profiles,
        }
    )


# =====================================================================
# audit list / cleanup (R6)
# =====================================================================


@audit_app.command("list")
def audit_list_cmd(
    apple_id: str | None = typer.Option(None, "--apple-id"),
    event_type: str | None = typer.Option(None, "--event-type"),
    since: str | None = typer.Option(
        None, "--since", help="ISO 8601 lower bound."
    ),
    limit: int = typer.Option(100, "--limit"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """List audit events ordered DESC theo timestamp (R6.4)."""
    _, _, audit_repo = _init_repos(db_path)
    rows = audit_repo.list(
        apple_id=apple_id,
        event_type=event_type,
        since=since,
        limit=limit,
    )
    _emit_json(rows)


@audit_app.command("cleanup")
def audit_cleanup_cmd(
    days: int = typer.Option(..., "--days", help="Xóa event cũ hơn N ngày."),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Cleanup audit event > days (R6.5)."""
    _, _, audit_repo = _init_repos(db_path)
    deleted = audit_repo.cleanup_older_than(days)
    _emit_json({"ok": True, "deleted": deleted, "days": days})


# =====================================================================
# profile delete (R5)
# =====================================================================


@profile_app.command("delete")
def profile_delete_cmd(
    apple_id: str = typer.Option(..., "--apple-id"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Delete profile_dir trên disk + DB status='deleted' (R5)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=None)
    result = pool_mgr.delete_profile(apple_id)
    _emit_json(
        {
            "apple_id": result.apple_id,
            "deleted": result.deleted,
            "profile_dir_removed": result.profile_dir_removed,
            "hme_count_at_delete": result.hme_count_at_delete,
            "reason": result.reason,
        }
    )


@profile_app.command("open")
def profile_open_cmd(
    apple_id: str = typer.Option(..., "--apple-id"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Mở profile EXISTING bằng Camoufox HEADED (R15.17).

    Blocking flow: launch Camoufox HEADED + đợi user nhấn Enter để Save (verify
    cookies + reactivate nếu session_expired/disabled/limited/quota_full), hoặc
    gõ 'q' + Enter để Close (đóng browser, KHÔNG sửa DB).

    Acquire ``Profile_Lock`` write mode (timeout 5s) — fail → exit 1.
    """
    settings = load_settings()
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("profile_open")

    try:
        result = asyncio.run(
            _run_profile_open(
                apple_id=apple_id,
                runtime_dir=settings.runtime_dir,
                pool_repo=pool_repo,
                audit_repo=audit_repo,
                log=log,
            )
        )
    except KeyboardInterrupt:
        typer.echo("[error] interrupted", err=True)
        raise typer.Exit(130)
    except Exception as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc

    _emit_json(result)


async def _run_profile_open(
    *,
    apple_id: str,
    runtime_dir,
    pool_repo,
    audit_repo,
    log,
) -> dict:
    """CLI orchestration cho `profile open` (R15.17, R15.18).

    Tách hàm async ra để asyncio.run wrap gọn. Pattern blocking giống
    Bootstrap_Flow CLI: input() chờ Enter / 'q'.
    """
    from .exceptions import OpenProfileError, ProfileLockError
    from .profile_lock import ProfileLock
    from .session import launch_camoufox

    apple_id_norm = (apple_id or "").strip().lower()
    if not apple_id_norm or "@" not in apple_id_norm:
        raise OpenProfileError(
            reason="profile_not_found",
            message=f"apple_id phải là email: {apple_id!r}",
            apple_id=apple_id,
        )

    account = pool_repo.get(apple_id_norm)
    if account is None or account.status == "deleted" or account.profile_dir is None:
        raise OpenProfileError(
            reason="profile_not_found",
            message=f"Profile {apple_id_norm} không tồn tại hoặc đã xoá",
            apple_id=apple_id_norm,
        )
    profile_dir = Path(account.profile_dir)
    if not profile_dir.exists():
        raise OpenProfileError(
            reason="profile_not_found",
            message=f"profile_dir không tồn tại: {profile_dir}",
            apple_id=apple_id_norm,
        )

    log(f"apple_id={apple_id_norm} profile_dir={profile_dir} previous_status={account.status}")

    lock_dir = profile_dir / ".lock"
    profile_lock = ProfileLock(lock_dir, apple_id_norm)
    lock_ctx = None
    try:
        try:
            lock_ctx = profile_lock.write_lock(timeout=5.0)
            lock_ctx.__enter__()
        except ProfileLockError as exc:
            audit_repo.write(
                event_type="profile_reopen_fail",
                apple_id=apple_id_norm,
                payload={"reason": "profile_locked", "mode": "cli", "error": str(exc)},
            )
            raise OpenProfileError(
                reason="profile_locked",
                message=(
                    f"Profile {apple_id_norm} đang được dùng bởi flow khác. "
                    f"Đợi bootstrap/recorder/open khác hoàn tất."
                ),
                apple_id=apple_id_norm,
            ) from exc

        audit_repo.write(
            event_type="profile_reopen_start",
            apple_id=apple_id_norm,
            payload={
                "mode": "cli",
                "profile_dir": str(profile_dir),
                "previous_status": account.status,
            },
        )

        async with launch_camoufox(
            profile_dir=profile_dir, headless=False, proxy=None
        ) as ctx:
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(
                    "https://www.icloud.com/mail/",
                    wait_until="domcontentloaded",
                )
            except Exception as exc:  # noqa: BLE001
                log(f"navigate warning: {exc!r}")

            print()
            print("=" * 70)
            print(f" Open profile flow — {apple_id_norm} (status={account.status})")
            print("=" * 70)
            print(" 1. Camoufox đã mở. Kiểm tra trạng thái session bằng mắt.")
            print(" 2. Nếu Apple bắt login lại / 2FA, hoàn tất tay trong cửa sổ.")
            print(" 3. Quay lại terminal:")
            print("    - Nhấn Enter để LƯU (verify cookies + reactivate nếu cần).")
            print("    - Gõ 'q' + Enter để ĐÓNG (không đổi DB).")
            print("=" * 70)

            answer = await asyncio.to_thread(_blocking_input, "[profile_open] Enter / 'q': ")

            if answer == "q":
                # Close path — KHÔNG verify, KHÔNG sửa DB.
                duration_sec = 0.0  # CLI không track precise; Web mode mới track
                audit_repo.write(
                    event_type="profile_reopen_close",
                    apple_id=apple_id_norm,
                    payload={"mode": "cli", "reason": "user_close"},
                )
                log(f"closed apple_id={apple_id_norm} (no DB change)")
                return {
                    "apple_id": apple_id_norm,
                    "action": "close",
                    "previous_status": account.status,
                    "current_status": account.status,
                }

            # Save path — verify cookies + reactivate.
            try:
                cookies = await ctx.cookies("https://www.icloud.com/")
            except Exception as exc:  # noqa: BLE001
                audit_repo.write(
                    event_type="profile_reopen_fail",
                    apple_id=apple_id_norm,
                    payload={"mode": "cli", "reason": "cookies_not_ready", "error": str(exc)},
                )
                raise OpenProfileError(
                    reason="cookies_not_ready",
                    message=f"Đọc cookies fail: {exc}",
                    apple_id=apple_id_norm,
                ) from exc

            markers = ("X-APPLE-WEBAUTH-USER", "X-APPLE-WEBAUTH-TOKEN", "X-APPLE-WEBAUTH-PCS-Mail")
            names = {c.get("name") for c in cookies if c.get("name")}
            matched = sorted(names & set(markers))
            if not matched:
                audit_repo.write(
                    event_type="profile_reopen_fail",
                    apple_id=apple_id_norm,
                    payload={
                        "mode": "cli",
                        "reason": "cookies_not_ready",
                        "recoverable": False,
                    },
                )
                raise OpenProfileError(
                    reason="cookies_not_ready",
                    message=(
                        f"Không tìm thấy cookie login marker nào. Hoàn tất login "
                        f"+ 2FA trong Camoufox trước khi nhấn Enter để Lưu."
                    ),
                    apple_id=apple_id_norm,
                )

            log(f"verify OK matched={matched}")

        # Camoufox closed (out of `async with`). Persist save trong outer-tx.
        engine = pool_repo.engine
        previous_status = account.status
        with engine.transaction() as _conn:
            pool_repo.upsert(apple_id_norm, profile_dir)
            pool_repo.update_status(
                apple_id_norm,
                status="active",
                clear_error=True,
                clear_limited_until=True,
                clear_quota_retry_until=True,
            )
            audit_repo.write(
                event_type="profile_reopen_save",
                apple_id=apple_id_norm,
                payload={
                    "mode": "cli",
                    "matched_cookies": matched,
                    "previous_status": previous_status,
                },
            )
            if previous_status in ("session_expired", "disabled", "limited", "quota_full"):
                audit_repo.write(
                    event_type="profile_reactivate",
                    apple_id=apple_id_norm,
                    payload={
                        "previous_status": previous_status,
                        "trigger": "open_profile_save_cli",
                    },
                )
        log(f"saved apple_id={apple_id_norm} previous_status={previous_status} → active")
        return {
            "apple_id": apple_id_norm,
            "action": "save",
            "previous_status": previous_status,
            "current_status": "active",
            "matched_cookies": matched,
        }
    finally:
        if lock_ctx is not None:
            try:
                lock_ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def _blocking_input(prompt: str) -> str:
    """Stdin read trên thread khác. EOF/error → 'q' (close path)."""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


# =====================================================================
# reconcile (R8)
# =====================================================================


@app.command("reconcile")
def reconcile_cmd(
    apple_id: str = typer.Option(..., "--apple-id"),
    proxy: str | None = typer.Option(None, "--proxy"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Sync DB ↔ Apple HME list (R8.3, R8.4)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("reconcile")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    generator = HmeGenerator(pool_mgr, pool_repo, audit_repo, log=log)
    try:
        added = asyncio.run(generator.reconcile(apple_id, proxy=proxy))
        _emit_json({"ok": True, "apple_id": apple_id, "added": added})
    except Exception as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc


# =====================================================================
# recording start / stop (R1)
# =====================================================================


@recording_app.command("start")
def recording_start_cmd(
    apple_id: str = typer.Option(..., "--apple-id"),
    scenario: str = typer.Option(..., "--scenario"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Bắt đầu Recording_Session (R1.1).

    NOTE (A13 review): Recorder skeleton chưa wire Camoufox launcher real.
    Command này hiện sẽ fail-fast với ``RecorderError(camoufox_launcher_fn
    chưa inject)`` thay vì silently no-op như trước. Wire launcher khi
    feature R1 ra khỏi MVP.
    """
    settings = load_settings()
    _, _, audit_repo = _init_repos(db_path)
    log = _emit_log("recording")
    recorder = Recorder(
        runtime_dir=settings.runtime_dir,
        audit_repo=audit_repo,
        retention_days=settings.icloud_recording_retention_days,
        log=log,
    )
    try:
        session = asyncio.run(recorder.start_session(apple_id, scenario=scenario))
    except RecorderError as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc
    _emit_json(
        {
            "session_id": session.session_id,
            "apple_id": session.apple_id,
            "scenario": session.scenario,
            "recording_dir": str(session.recording_dir),
            "started_at": session.started_at.isoformat() + "Z",
        }
    )


@recording_app.command("stop")
def recording_stop_cmd(
    session_id: str = typer.Option(..., "--session-id"),
    exit_reason: str = typer.Option("normal", "--exit-reason"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Stop Recording_Session (R1.5)."""
    settings = load_settings()
    _, _, audit_repo = _init_repos(db_path)
    log = _emit_log("recording")
    recorder = Recorder(
        runtime_dir=settings.runtime_dir,
        audit_repo=audit_repo,
        retention_days=settings.icloud_recording_retention_days,
        log=log,
    )
    try:
        session = asyncio.run(
            recorder.stop_session(session_id, exit_reason=exit_reason)
        )
    except RecorderError as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc
    _emit_json(
        {
            "session_id": session.session_id,
            "apple_id": session.apple_id,
            "scenario": session.scenario,
            "ended_at": session.ended_at.isoformat() + "Z" if session.ended_at else None,
            "exit_reason": session.exit_reason,
        }
    )


# =====================================================================
# email lifecycle (R9 — phase sau MVP)
# =====================================================================


@email_app.command("deactivate")
def email_deactivate_cmd(
    email: str = typer.Option(..., "--email"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Deactivate 1 email HME (R9.1)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("email")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    from .manager import HmeManager

    hme_mgr = HmeManager(pool_mgr, pool_repo, audit_repo, log=log)
    try:
        result = asyncio.run(hme_mgr.deactivate(email, dry_run=dry_run))
        _emit_lifecycle(result)
        if result.succeeded == 0 and not dry_run:
            raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc


@email_app.command("reactivate")
def email_reactivate_cmd(
    email: str = typer.Option(..., "--email"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Reactivate 1 email HME (R9.13)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("email")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    from .manager import HmeManager

    hme_mgr = HmeManager(pool_mgr, pool_repo, audit_repo, log=log)
    try:
        result = asyncio.run(hme_mgr.reactivate(email, dry_run=dry_run))
        _emit_lifecycle(result)
        if result.succeeded == 0 and not dry_run:
            raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc


@email_app.command("delete")
def email_delete_cmd(
    email: str = typer.Option(..., "--email"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Delete 1 email HME (R9.14)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("email")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    from .manager import HmeManager

    hme_mgr = HmeManager(pool_mgr, pool_repo, audit_repo, log=log)
    try:
        result = asyncio.run(hme_mgr.delete(email, dry_run=dry_run))
        _emit_lifecycle(result)
        if result.succeeded == 0 and not dry_run:
            raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc


@email_app.command("mark-used")
def email_mark_used_cmd(
    email: str = typer.Option(..., "--email"),
    used_for: str = typer.Option(..., "--used-for"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Mark email used for ChatGPT signup (DB-only, R9.19)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=None)
    from .manager import HmeManager

    hme_mgr = HmeManager(pool_mgr, pool_repo, audit_repo, log=None)
    result = asyncio.run(hme_mgr.mark_used(email, used_for=used_for))
    _emit_lifecycle(result)


@email_app.command("update-meta")
def email_update_meta_cmd(
    email: str = typer.Option(..., "--email"),
    label: str | None = typer.Option(None, "--label"),
    note: str | None = typer.Option(None, "--note"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Update label/note 1 email HME (R9.16)."""
    if label is None and note is None:
        typer.echo("[error] cần --label hoặc --note", err=True)
        raise typer.Exit(2)
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("email")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    from .manager import HmeManager

    hme_mgr = HmeManager(pool_mgr, pool_repo, audit_repo, log=log)
    try:
        result = asyncio.run(
            hme_mgr.update_meta(email, label=label, note=note, dry_run=dry_run)
        )
        _emit_lifecycle(result)
        if result.succeeded == 0 and not dry_run:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"[error] {exc}", err=True)
        raise typer.Exit(1) from exc


@email_app.command("list")
def email_list_cmd(
    status: str | None = typer.Option(None, "--status"),
    apple_id: str | None = typer.Option(None, "--apple-id"),
    label: str | None = typer.Option(None, "--label"),
    limit: int | None = typer.Option(None, "--limit"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """List icloud_emails với filter status/apple_id/label (R9.20 query)."""
    _, pool_repo, _ = _init_repos(db_path)
    rows = pool_repo.list_emails(
        status=status,
        apple_id=apple_id,
        label=label,
        limit=limit,
    )
    _emit_json(rows)


@email_app.command("export")
def email_export_cmd(
    format_: str = typer.Option("json", "--format", help="csv hoặc json."),
    status: str | None = typer.Option(None, "--status"),
    apple_id: str | None = typer.Option(None, "--apple-id"),
    label: str | None = typer.Option(None, "--label"),
    output: str | None = typer.Option(
        None, "--output", help="File path để ghi. Bỏ trống → stdout."
    ),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Export icloud_emails sang csv|json (R9.20)."""
    fmt = format_.lower()
    if fmt not in ("csv", "json"):
        typer.echo(f"[error] --format phải csv hoặc json, got {format_!r}", err=True)
        raise typer.Exit(2)

    _, pool_repo, audit_repo = _init_repos(db_path)
    rows = pool_repo.list_emails(
        status=status,
        apple_id=apple_id,
        label=label,
    )

    # Normalize rows → list[dict] với key ổn định.
    columns = [
        "id",
        "email",
        "apple_id",
        "label",
        "note",
        "hme_id",
        "status",
        "created_at",
        "deactivated_at",
        "reactivated_at",
        "deleted_at",
        "last_sync_at",
        "used_for_email",
    ]
    normalized = [{c: r.get(c) for c in columns} for r in rows]

    if fmt == "json":
        body = json.dumps(normalized, ensure_ascii=False, indent=2, default=str)
    else:
        import csv
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        for row in normalized:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
        body = buf.getvalue()

    audit_repo.write(
        event_type="email_export",
        apple_id=apple_id,
        payload={
            "count": len(normalized),
            "format": fmt,
            "filter": {
                "status": status,
                "apple_id": apple_id,
                "label": label,
            },
            "output": output,
        },
    )

    if output:
        Path(output).write_text(body, encoding="utf-8")
        _emit_json(
            {
                "ok": True,
                "count": len(normalized),
                "format": fmt,
                "output": output,
            }
        )
    else:
        # Body raw ra stdout (csv dạng string, json dạng pretty); KHÔNG bọc thêm.
        typer.echo(body)


@email_app.command("list-sync")
def email_list_sync_cmd(
    apple_id: str = typer.Option(..., "--apple-id"),
    db_path: str = typer.Option("runtime/data.db", "--db-path"),
) -> None:
    """Sync DB ↔ Apple HME list 5 nhánh (R9.12)."""
    _, pool_repo, audit_repo = _init_repos(db_path)
    log = _emit_log("sync")
    pool_mgr = IcloudPoolManager(pool_repo, audit_repo, log=log)
    from .manager import HmeManager

    hme_mgr = HmeManager(pool_mgr, pool_repo, audit_repo, log=log)
    diff = asyncio.run(hme_mgr.list_sync(apple_id))
    _emit_json(
        {
            "apple_id": diff.apple_id,
            "inserted_active": diff.inserted_active,
            "inserted_inactive": diff.inserted_inactive,
            "db_marked_deactivated": diff.db_marked_deactivated,
            "db_marked_deleted": diff.db_marked_deleted,
            "db_marked_reactivated": diff.db_marked_reactivated,
            "unchanged": diff.unchanged,
        }
    )


def _emit_lifecycle(result) -> None:
    _emit_json(
        {
            "requested": result.requested,
            "succeeded": result.succeeded,
            "skipped": result.skipped,
            "remaining": result.remaining,
            "failed": result.failed,
            "dry_run": result.dry_run,
        }
    )


def main() -> None:
    """Entry point — gọi từ ``__main__.py``."""
    app()


if __name__ == "__main__":
    main()
