"""iCloud Hide My Email pool — feature ``icloud-hme-pool``.

Public API surface (re-exports cho user code import từ ``icloud_hme``):

Service layer (R2-R8, R12):
    IcloudPoolManager  — Pool state machine + round-robin pick (R2, R5, R7).
    HmeGenerator       — HME email generation orchestrator (R3, R8).
    ProfileChecker     — Session validity probe (R4).
    Recorder           — Camoufox tracing recorder cho discovery (R1).
    bootstrap          — Bootstrap_Flow login flow (R12.2).

Infrastructure:
    HmeClient          — httpx async client cho 7 Apple HME endpoint (R11).
    extract_session_bundle — Session_Bundle extractor (R12.3).
    ProfileLock        — RW lock per Apple_ID (R12.14-R12.16).

Models:
    SessionBundle, AppleAccount, GenerationResult, CheckResult,
    PoolStatusReport, ProfileSnapshot, ProfileDeleteResult, FailureRecord,
    LifecycleResult, SyncDiff, ExportResult, GeneratedCandidate, ReservedHme,
    RemoteHme, RecordingSession, BootstrapResult.

Exceptions:
    IcloudError (base), IcloudPoolError, BootstrapError, SessionExtractError,
    TerminalStatusError, HmeClientError + subclasses, ProfileLockError,
    RecorderError.

Backward-compat (legacy `IcloudAccountRepository` + `IcloudEmailRepository`
trong ``icloud_hme.repository``) vẫn export để CLI cũ / test cũ chạy được.
Khuyến nghị: code mới SHALL dùng ``IcloudPoolRepository`` từ ``db.repositories``.
"""

# ── Service layer ────────────────────────────────────────────────────────
from .bootstrap import bootstrap  # noqa: F401
from .checker import ProfileChecker  # noqa: F401
from .generator import HmeGenerator  # noqa: F401
from .open_profile import (  # noqa: F401
    OpenProfileService,
    OpenProfileSession,
    OpenProfileState,
)
from .pool import IcloudPoolManager  # noqa: F401
from .recorder import Recorder, RecorderError  # noqa: F401

# ── Infrastructure ───────────────────────────────────────────────────────
from .client import HmeClient, classify_response  # noqa: F401
from .profile_lock import ProfileLock  # noqa: F401
from .session import (  # noqa: F401
    ensure_profile_dir,
    extract_session_bundle,
    launch_camoufox,
    profile_dir_for,
)

# ── Models ───────────────────────────────────────────────────────────────
from .models import (  # noqa: F401
    AppleAccount,
    BootstrapResult,
    CheckResult,
    ExportResult,
    FailureRecord,
    GeneratedCandidate,
    GenerationResult,
    LifecycleResult,
    PoolStatusReport,
    ProfileDeleteResult,
    ProfileSnapshot,
    RecordingSession,
    RemoteHme,
    ReservedHme,
    SessionBundle,
    SyncDiff,
)

# ── Exceptions ───────────────────────────────────────────────────────────
from .exceptions import (  # noqa: F401
    BootstrapError,
    HmeAuthError,
    HmeClientError,
    HmeNotFoundError,
    HmeQuotaError,
    HmeReserveTaken,
    HmeTransientError,
    IcloudError,
    IcloudPoolError,
    OpenProfileError,
    ProfileLockError,
    SessionExtractError,
    TerminalStatusError,
)

# ── Legacy backward-compat ───────────────────────────────────────────────
# Giữ lại để CLI cũ / hotmail flow nhập từ `icloud_hme` không bị vỡ.
# Code mới SHALL dùng `IcloudPoolRepository` + `AuditLogRepository`.
from .repository import (  # noqa: F401
    IcloudAccountRepository,
    IcloudEmailRepository,
    IcloudRepositoryError,
)


__all__ = [
    # Service
    "IcloudPoolManager",
    "HmeGenerator",
    "OpenProfileService",
    "OpenProfileSession",
    "OpenProfileState",
    "ProfileChecker",
    "Recorder",
    "RecorderError",
    "bootstrap",
    # Infrastructure
    "HmeClient",
    "classify_response",
    "ProfileLock",
    "ensure_profile_dir",
    "extract_session_bundle",
    "launch_camoufox",
    "profile_dir_for",
    # Models
    "AppleAccount",
    "BootstrapResult",
    "CheckResult",
    "ExportResult",
    "FailureRecord",
    "GeneratedCandidate",
    "GenerationResult",
    "LifecycleResult",
    "PoolStatusReport",
    "ProfileDeleteResult",
    "ProfileSnapshot",
    "RecordingSession",
    "RemoteHme",
    "ReservedHme",
    "SessionBundle",
    "SyncDiff",
    # Exceptions
    "BootstrapError",
    "HmeAuthError",
    "HmeClientError",
    "HmeNotFoundError",
    "HmeQuotaError",
    "HmeReserveTaken",
    "HmeTransientError",
    "IcloudError",
    "IcloudPoolError",
    "OpenProfileError",
    "ProfileLockError",
    "SessionExtractError",
    "TerminalStatusError",
    # Legacy
    "IcloudAccountRepository",
    "IcloudEmailRepository",
    "IcloudRepositoryError",
]
