"""In-memory dataclasses cho feature `icloud-hme-pool`.

Reference: `.kiro/specs/icloud-hme-pool/design.md` §Data Models / §Components.

Module này KHÔNG phụ thuộc vào ``icloud_hme.exceptions`` (tránh circular import)
và KHÔNG re-export ngoài. Pool / Generator / HmeClient / HME_Manager / Runner
import trực tiếp từ ``icloud_hme.models``.

Frozen dataclass (immutable):
    - ``SessionBundle``  — Session_Bundle Extractor output (R12.4).
    - ``AppleAccount``   — snapshot 1 row ``icloud_accounts`` đọc từ Pool_Repository (R2).

Mutable dataclass (service result):
    - ``GenerationResult``, ``CheckResult``, ``ProfileSnapshot``, ``PoolStatusReport``,
      ``ProfileDeleteResult``, ``FailureRecord``, ``LifecycleResult``, ``SyncDiff``,
      ``ExportResult``.

HmeClient response dataclass:
    - ``GeneratedCandidate``, ``ReservedHme``, ``RemoteHme`` (R11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Frozen dataclass — immutable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionBundle:
    """Session bundle extract từ Camoufox profile_dir (R12.4 — cookies-only).

    Bundle in-memory only, KHÔNG được serialize ra disk (R12.6, Property 17).
    Apple HME API auth duy nhất qua cookies (X-APPLE-WEBAUTH-*); các field
    cũ (``dsid``, ``client_id``, ``scnt``, ``x_apple_id_session_id``,
    ``maildomainws_host``, ``user_agent``) đã bị bỏ vì:

      - ``window.webAuth`` không còn được Apple webapp expose ở
        ``icloud.com`` hiện tại — extract qua ``page.evaluate`` luôn fail.
      - ``dsid`` / ``client_id`` không bắt buộc trong query param Apple HME
        (truyền chuỗi rỗng vẫn 200 + ``success=true``).
      - ``scnt`` / ``X-Apple-ID-Session-Id`` headers không cần khi cookies
        đầy đủ.
      - ``maildomainws_host`` cố định ``p68-maildomainws.icloud.com`` cho
        mọi account (verified với rtunazzz/hidemyemail-generator + check
        nội bộ ``test/check_hme_minimal_call.py``).

    Validate ở extractor: ``cookies`` non-empty + có ÍT NHẤT 1 trong các
    marker login ``X-APPLE-WEBAUTH-USER`` / ``X-APPLE-WEBAUTH-TOKEN`` /
    ``X-APPLE-WEBAUTH-PCS-Mail``; thiếu → ``SessionExtractError`` (R12.5,
    Property 16).
    """

    apple_id: str
    cookies: dict[str, str]
    extracted_at: datetime  # UTC


@dataclass(frozen=True)
class BootstrapResult:
    """Kết quả ``Bootstrap_Flow.bootstrap`` (R12.1, R12.2, R12.10).

    Trả về sau khi user đã login + 2FA tay xong, cookies verify pass và
    DB đã upsert + audit ghi xong. Frozen vì là output snapshot, caller
    không nên mutate.
    """

    apple_id: str
    profile_dir: Path
    status: str  # luôn 'active' khi bootstrap thành công
    matched_cookies: list[str]  # tên cookie marker đã xác thực (sorted)
    bootstrapped_at: datetime  # UTC


@dataclass(frozen=True)
class AppleAccount:
    """Snapshot 1 row ``icloud_accounts`` (R2)."""

    apple_id: str
    profile_dir: Path | None
    status: str  # active|limited|quota_full|session_expired|disabled|deleted
    hme_count: int
    limited_until: datetime | None
    quota_retry_until: datetime | None  # non-null khi status='quota_full' (R2.10-R2.12)
    last_used_at: datetime | None
    last_error: str | None


# ---------------------------------------------------------------------------
# Service result dataclass — mutable
# ---------------------------------------------------------------------------


@dataclass
class FailureRecord:
    """1 entry failure trong ``GenerationResult.failures`` (R3)."""

    apple_id: str
    error_class: str
    error: str


@dataclass
class GenerationResult:
    """Kết quả ``HME_Generator.generate`` (R3, R8)."""

    requested: int
    created: int
    emails: list[str] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    disabled_profiles: list[str] = field(default_factory=list)
    label: str = ""


@dataclass
class CheckResult:
    """Kết quả ``Profile_Checker.check_one`` (R4)."""

    apple_id: str
    ok: bool
    status: str  # active|limited|session_expired|missing_profile|error
    hme_count_remote: int | None
    hme_count_local: int
    error: str | None
    error_class: str | None


@dataclass
class ProfileSnapshot:
    """1 entry trong ``PoolStatusReport.profiles`` (R7)."""

    apple_id: str
    status: str
    hme_count: int
    quota_remaining: int
    last_used_at: datetime | None
    limited_until: datetime | None
    quota_retry_until: datetime | None  # non-null khi status='quota_full' (R7.2)
    last_error: str | None


@dataclass
class PoolStatusReport:
    """Aggregated pool status report (R7.1, R7.5)."""

    by_status: dict[str, int]
    profiles: list[ProfileSnapshot]
    emails_by_status: dict[str, int]
    quota_soft_cap_per_account: int
    total_quota_remaining: int
    low_capacity: bool
    quota_full_count: int
    quota_full_profiles: list[dict]  # [{apple_id, hme_count, quota_retry_until}]


@dataclass
class ProfileDeleteResult:
    """Kết quả ``Pool_Manager.delete_profile`` (R5)."""

    apple_id: str
    deleted: bool
    profile_dir_removed: bool
    hme_count_at_delete: int
    reason: str | None


@dataclass
class LifecycleResult:
    """Kết quả ``HME_Manager.<lifecycle action>`` (R9.1, R9.13, R9.14)."""

    requested: int
    succeeded: int
    skipped: list[dict] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class SyncDiff:
    """Kết quả ``HME_Manager.list_sync`` — 3 nhánh UPDATE diff (R9.12).

    Refactor B (DB source-of-truth): chỉ UPDATE email DB-side đã có dựa
    trên Apple state; KHÔNG insert email apple-side vào DB. Field
    ``inserted_active`` + ``inserted_inactive`` giữ = 0 cho backward-compat
    với caller cũ.
    """

    apple_id: str
    inserted_active: int = 0  # luôn 0 sau refactor B (legacy field)
    inserted_inactive: int = 0  # luôn 0 sau refactor B (legacy field)
    db_marked_deactivated: int = 0
    db_marked_deleted: int = 0
    db_marked_reactivated: int = 0
    unchanged: int = 0


@dataclass
class ExportResult:
    """Kết quả ``HME_Manager.export`` (R9)."""

    count: int
    format: str  # 'csv' | 'json'
    output_path: Path | None
    audit_logged: bool


# ---------------------------------------------------------------------------
# HmeClient response dataclass (R11)
# ---------------------------------------------------------------------------


@dataclass
class GeneratedCandidate:
    """Response của ``HmeClient.generate`` — candidate chưa reserve (R3.13)."""

    candidate: str  # email chưa reserve
    raw: dict


@dataclass
class ReservedHme:
    """Response của ``HmeClient.reserve`` — đã chốt slot Apple-side (R3)."""

    email: str
    hme_id: str  # anonymousId (hoặc hmeId nếu Apple trả)
    label: str | None
    note: str | None
    raw: dict


@dataclass
class RemoteHme:
    """1 entry trong ``HmeClient.list`` response — Apple-side state (R8, R9.12)."""

    email: str
    hme_id: str
    label: str | None
    note: str | None
    is_active: bool
    create_timestamp: int


@dataclass
class RecordingSession:
    """1 lượt user mở Camoufox manual để record (R1).

    Fields theo design.md §Components / 8. Recorder:
        session_id: uuid4 hex
        apple_id: Apple ID dùng cho profile_dir
        scenario: tag user đặt — 'create' | 'revoke' | ...
        recording_dir: path tới ``runtime/icloud_recordings/<session_id>/``
        started_at / ended_at: UTC datetime
        exit_reason: 'normal' | 'crashed' | 'interrupted' (None khi đang chạy)
    """

    session_id: str
    apple_id: str
    scenario: str
    recording_dir: Path
    started_at: datetime
    ended_at: datetime | None
    exit_reason: str | None
