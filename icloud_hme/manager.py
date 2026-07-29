"""HME_Manager — quản lý vòng đời HME email Apple-side (R9, sau MVP).

Refs:
    requirements.md R9.1–R9.20
    design.md §Components / 7. HME_Manager + Sequence email lifecycle + list_sync
    tasks.md task 23

Class :class:`HmeManager` orchestrate post-create lifecycle cho HME email:
    - 4 single-action Apple-side: ``deactivate / reactivate / delete /
      update_meta`` (R9.1, R9.13, R9.14, R9.16).
    - 1 DB-only action: ``mark_used`` (R9.19).
    - 1 sync action: ``list_sync(apple_id)`` với 5 nhánh diff (R9.12).
    - Bulk variants (``deactivate_bulk`` / ``reactivate_bulk`` /
      ``delete_bulk`` / ``update_meta_bulk``) — group by apple_id chủ +
      reuse SessionBundle in-memory + delay random ``[delay_min, delay_max]``s
      giữa cặp request kế tiếp (R9.7, R9.17).

Mọi mutation DB đi cùng audit event trong CÙNG outer tx (R6.3, R9 audit
contract). ``dry_run=True`` → KHÔNG gọi API, KHÔNG UPDATE DB, KHÔNG ghi
audit lifecycle (R9.18).

Match key cho ``list_sync``: ``hme_id`` (= ``anonymousId`` Apple-side).
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from .client import HmeClient
from .exceptions import (
    HmeAuthError,
    HmeClientError,
    HmeNotFoundError,
    HmeQuotaError,
    SessionExtractError,
    TerminalStatusError,
)
from .models import (
    LifecycleResult,
    SessionBundle,
    SyncDiff,
)
from .session import extract_session_bundle

if TYPE_CHECKING:
    from db.repositories import AuditLogRepository, IcloudPoolRepository

    from .pool import IcloudPoolManager


# ---------------------------------------------------------------------------
# Action contracts (Property 18, R9.13, R9.14, R9.16)
# ---------------------------------------------------------------------------

# Status ENUM (v6 schema): created|reconciled|deactivated|revoked|deleted|
# disabled|used_for_chatgpt.

# Acceptable preconditions (status hiện tại HỢP LỆ cho action). Mọi status
# khác → terminal cho action đó → raise TerminalStatusError, không gọi API.
_ACCEPTABLE_STATUSES: dict[str, frozenset[str]] = {
    "deactivate": frozenset({"created", "reconciled"}),
    "reactivate": frozenset({"deactivated", "revoked"}),
    # delete: chấp nhận mọi status NGOẠI TRỪ 'deleted' (Property 18).
    "delete": frozenset(
        {
            "created",
            "reconciled",
            "deactivated",
            "revoked",
            "disabled",
            "used_for_chatgpt",
        }
    ),
    # update_meta: chấp nhận mọi status NGOẠI TRỪ 'deleted' (R9.16, Property 18).
    "update_meta": frozenset(
        {
            "created",
            "reconciled",
            "deactivated",
            "revoked",
            "disabled",
            "used_for_chatgpt",
        }
    ),
}

# Audit event names per action (R9 audit contract).
_AUDIT_SUCCESS_BY_ACTION: dict[str, str] = {
    "deactivate": "email_deactivate",
    "reactivate": "email_reactivate",
    "delete": "email_delete",
    "update_meta": "email_update_meta",
}
_AUDIT_FAIL_BY_ACTION: dict[str, str] = {
    "deactivate": "email_deactivate_fail",
    "reactivate": "email_reactivate_fail",
    "delete": "email_delete_fail",
    "update_meta": "email_update_meta_fail",
}

# Default delay giữa 2 request kế tiếp trong group bulk (R9.7).
_DEFAULT_DELAY_RANGE: tuple[float, float] = (1.0, 3.0)


def _utc_now() -> datetime:
    """UTC naive datetime — match Timestamp_Format (Property 30)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Class
# ---------------------------------------------------------------------------


class HmeManager:
    """Full lifecycle layer cho HME email post-create (R9).

    Args:
        pool: ``IcloudPoolManager`` cho mark_limited / mark_session_expired
            khi gặp quota / auth error giữa group.
        pool_repo: ``IcloudPoolRepository`` cho UPDATE icloud_emails +
            INSERT icloud_emails (list_sync) + lookup profile.
        audit_repo: ``AuditLogRepository`` cho audit ghi cùng outer tx.
        delay_range: ``(min, max)`` seconds random delay giữa 2 request kế
            tiếp trong group bulk (R9.7). Default ``(1.0, 3.0)``.
        log: optional logger callable ``(msg) -> None``.
        extract_session_bundle_fn: inject để test mock; default = real
            ``extract_session_bundle`` (R12.3).
        client_factory: inject để test mock; default = real ``HmeClient``.
        sleep_fn: inject async sleep callable; default = ``asyncio.sleep``.
            Mock để bypass real wait + đo delay logical (Property 13).
    """

    def __init__(
        self,
        pool: "IcloudPoolManager",
        pool_repo: "IcloudPoolRepository",
        audit_repo: "AuditLogRepository",
        *,
        delay_range: tuple[float, float] = _DEFAULT_DELAY_RANGE,
        log: Any = None,
        extract_session_bundle_fn: Callable[..., Any] | None = None,
        client_factory: Callable[..., HmeClient] | None = None,
        sleep_fn: Callable[[float], Any] | None = None,
    ) -> None:
        if delay_range[0] < 0 or delay_range[1] < delay_range[0]:
            raise ValueError(f"delay_range không hợp lệ: {delay_range}")
        self._pool = pool
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._delay_range = delay_range
        self._log = log if callable(log) else (lambda *_a, **_k: None)
        self._extract_fn = extract_session_bundle_fn or extract_session_bundle
        self._client_factory = client_factory or (
            lambda bundle: HmeClient(bundle, log=self._log)
        )
        self._sleep = sleep_fn or asyncio.sleep

    # =====================================================================
    # Single email actions
    # =====================================================================

    async def deactivate(
        self, email: str, *, dry_run: bool = False
    ) -> LifecycleResult:
        """POST /v1/hme/deactivate (R9.1, R9.5, R9.6, R9.8)."""
        return await self._single_action(
            email, action="deactivate", dry_run=dry_run
        )

    async def reactivate(
        self, email: str, *, dry_run: bool = False
    ) -> LifecycleResult:
        """POST /v1/hme/reactivate (R9.13)."""
        return await self._single_action(
            email, action="reactivate", dry_run=dry_run
        )

    async def delete(
        self, email: str, *, dry_run: bool = False
    ) -> LifecycleResult:
        """POST /v1/hme/delete (R9.14, R9.15)."""
        return await self._single_action(
            email, action="delete", dry_run=dry_run
        )

    async def update_meta(
        self,
        email: str,
        *,
        label: str | None,
        note: str | None,
        dry_run: bool = False,
    ) -> LifecycleResult:
        """POST /v1/hme/updateMetaData — đổi label/note, không đổi status (R9.16)."""
        return await self._single_action(
            email,
            action="update_meta",
            dry_run=dry_run,
            extra={"label": label, "note": note},
        )

    async def mark_used(
        self, email: str, *, used_for: str
    ) -> LifecycleResult:
        """DB-only: status='used_for_chatgpt' + used_for_email (R9.19).

        SHALL NOT gọi API Apple. Audit ``email_mark_used`` cùng tx.
        """
        row = self._pool_repo.get_email(email)
        if row is None:
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[],
                remaining=[],
                failed=[
                    {
                        "email": email,
                        "reason": "email_not_found",
                        "error": f"email không tồn tại: {email}",
                    }
                ],
                dry_run=False,
            )
        if row["status"] == "used_for_chatgpt":
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[{"email": email, "reason": "already_used"}],
                remaining=[],
                failed=[],
                dry_run=False,
            )

        engine = self._pool_repo.engine
        with engine.transaction() as _conn:
            self._pool_repo.update_email_status(
                email,
                status="used_for_chatgpt",
                used_for_email=used_for,
            )
            self._audit_repo.write(
                event_type="email_mark_used",
                apple_id=row["apple_id"],
                payload={"email": email, "used_for": used_for},
            )
        return LifecycleResult(
            requested=1,
            succeeded=1,
            skipped=[],
            remaining=[],
            failed=[],
            dry_run=False,
        )

    # =====================================================================
    # Bulk actions (R9.2, R9.7, R9.17)
    # =====================================================================

    async def deactivate_bulk(
        self, emails: list[str], *, dry_run: bool = False
    ) -> LifecycleResult:
        return await self._bulk_action(
            emails, action="deactivate", dry_run=dry_run
        )

    async def reactivate_bulk(
        self, emails: list[str], *, dry_run: bool = False
    ) -> LifecycleResult:
        return await self._bulk_action(
            emails, action="reactivate", dry_run=dry_run
        )

    async def delete_bulk(
        self, emails: list[str], *, dry_run: bool = False
    ) -> LifecycleResult:
        return await self._bulk_action(
            emails, action="delete", dry_run=dry_run
        )

    async def update_meta_bulk(
        self, items: list[dict], *, dry_run: bool = False
    ) -> LifecycleResult:
        """items = [{email, label?, note?}]"""
        # Build map email → extra. Không group lại — bulk loop reuse session.
        emails: list[str] = []
        extras_by_email: dict[str, dict] = {}
        for item in items:
            email = item["email"]
            emails.append(email)
            extras_by_email[email] = {
                "label": item.get("label"),
                "note": item.get("note"),
            }
        return await self._bulk_action(
            emails,
            action="update_meta",
            dry_run=dry_run,
            extras_by_email=extras_by_email,
        )

    # =====================================================================
    # list_sync (R9.12)
    # =====================================================================

    async def list_sync(
        self, apple_id: str, *, dry_run: bool = False
    ) -> SyncDiff:
        """Pull /v2/hme/list, diff với DB-side, áp 3 nhánh UPDATE trong 1 tx.

        DB là source-of-truth. Email Apple-side mà DB không có → bỏ qua,
        KHÔNG insert. Logic này khớp scope tool: chỉ quản email do tool tạo
        (đã INSERT lúc generate) — email user tạo tay ngoài tool không phải
        domain của list_sync.

        3 nhánh diff (Property 19, design §list_sync flow — refactor B):
            3. apple inactive + db status ∈ {created, reconciled} →
               UPDATE status='deactivated' + deactivated_at + audit
               email_deactivate (reason='external_change').
            4a. apple missing + db status ∈ {created, reconciled} →
                UPDATE status='deleted' + deleted_at + audit email_delete
                (reason='external_change').
            4b. apple missing + db status='used_for_chatgpt' →
                UPDATE status='disabled' + audit email_delete
                (reason='apple_deleted_after_use'). HME này đã được dùng
                cho ChatGPT signup nhưng Apple đã xoá → forward không
                hoạt động, đánh dấu disabled để admin biết (A10 fix).
            5. apple active + db status ∈ {deactivated, revoked} → UPDATE
               status='created' + reactivated_at + audit email_reactivate
               (reason='external_change').

        Đã drop:
            1. apple active + db missing — bỏ qua, KHÔNG insert.
            2. apple inactive + db missing — bỏ qua, KHÔNG insert.

        ``SyncDiff.inserted_active`` + ``SyncDiff.inserted_inactive`` giữ
        field 0 luôn (backward-compat — caller cũ có thể đọc, sẽ thấy 0).

        Args:
            apple_id: profile cần sync.
            dry_run: True → tính diff KHÔNG ghi DB hoặc audit, trả counters
                để UI preview impact (B7 — review fix). False (default) →
                áp 3 nhánh UPDATE trong 1 transaction.

        Match key: ``hme_id`` (= anonymousId Apple-side).

        Returns:
            ``SyncDiff`` với 3 counter UPDATE + ``unchanged`` + 2 counter
            insert luôn = 0. Khi ``dry_run`` counters phản ánh "sẽ áp" thay
            vì "đã áp".
        """
        account = self._pool_repo.get(apple_id)
        if account is None:
            raise ValueError(f"apple_id không tồn tại: {apple_id}")
        if account.profile_dir is None:
            raise ValueError(f"profile_dir NULL cho apple_id={apple_id}")

        bundle = await self._extract_fn(
            profile_dir=account.profile_dir,
            apple_id=apple_id,
            audit_repo=self._audit_repo,
            proxy=None,
            log=self._log,
        )
        client = self._client_factory(bundle)
        try:
            remote_items = await client.list()
        finally:
            await client.aclose()

        # Build apple_side dict: {hme_id: is_active} (loại item không có hme_id).
        apple_by_hme_id: dict[str, bool] = {}
        for item in remote_items:
            if not item.hme_id:
                continue
            apple_by_hme_id[item.hme_id] = item.is_active
        # Refactor B (DB source-of-truth): KHÔNG còn cần meta để INSERT —
        # nhánh 1+2 đã drop. Giữ ``apple_by_hme_id`` (dict hme_id → is_active)
        # đủ cho các nhánh 3/4/5 transition.

        # Build db_side dict: {hme_id: row} (chỉ row có hme_id non-null/empty).
        db_rows = self._pool_repo.list_emails(apple_id=apple_id)
        db_by_hme_id: dict[str, dict] = {
            row["hme_id"]: row for row in db_rows if row.get("hme_id")
        }

        diff = SyncDiff(apple_id=apple_id)
        now = _utc_now()

        if dry_run:
            # Compute diff KHÔNG ghi — refactor B (DB source-of-truth):
            # KHÔNG count nhánh 1+2 (insert apple-side missing) vì list_sync
            # không INSERT. inserted_active / inserted_inactive luôn = 0.
            for hme_id, row in db_by_hme_id.items():
                db_status = row["status"]
                in_apple = hme_id in apple_by_hme_id
                apple_active = (
                    apple_by_hme_id[hme_id] if in_apple else None
                )
                if (
                    in_apple
                    and apple_active is False
                    and db_status in ("created", "reconciled")
                ):
                    diff.db_marked_deactivated += 1
                elif not in_apple and db_status in ("created", "reconciled"):
                    diff.db_marked_deleted += 1
                elif not in_apple and db_status == "used_for_chatgpt":
                    diff.db_marked_deleted += 1  # A10 — count chung với 4a
                elif (
                    in_apple
                    and apple_active is True
                    and db_status in ("deactivated", "revoked")
                ):
                    diff.db_marked_reactivated += 1
                else:
                    diff.unchanged += 1
            return diff

        engine = self._pool_repo.engine
        with engine.transaction() as _conn:
            # Refactor B (DB source-of-truth): KHÔNG insert email apple-side
            # vào DB. Chỉ scan db-side, áp transition theo apple-side state.
            for hme_id, row in db_by_hme_id.items():
                db_status = row["status"]
                in_apple = hme_id in apple_by_hme_id
                apple_active = (
                    apple_by_hme_id[hme_id] if in_apple else None
                )

                # Nhánh 3: apple inactive + db {created, reconciled}
                if (
                    in_apple
                    and apple_active is False
                    and db_status in ("created", "reconciled")
                ):
                    self._pool_repo.update_email_status(
                        row["email"],
                        status="deactivated",
                        deactivated_at=now,
                        last_sync_at=now,
                    )
                    self._audit_repo.write(
                        event_type="email_deactivate",
                        apple_id=apple_id,
                        payload={
                            "email": row["email"],
                            "hme_id": hme_id,
                            "reason": "external_change",
                        },
                    )
                    diff.db_marked_deactivated += 1
                    continue

                # Nhánh 4a: apple missing + db {created, reconciled}.
                if not in_apple and db_status in ("created", "reconciled"):
                    self._pool_repo.update_email_status(
                        row["email"],
                        status="deleted",
                        deleted_at=now,
                        last_sync_at=now,
                    )
                    self._audit_repo.write(
                        event_type="email_delete",
                        apple_id=apple_id,
                        payload={
                            "email": row["email"],
                            "hme_id": hme_id,
                            "reason": "external_change",
                        },
                    )
                    diff.db_marked_deleted += 1
                    continue

                # Nhánh 4b: apple missing + db status='used_for_chatgpt' (A10).
                # Email đã dùng cho ChatGPT signup nhưng Apple xoá → forward
                # không còn hoạt động → mark disabled để admin biết. KHÔNG
                # dùng status='deleted' vì email đã được consume cho 1 mục
                # đích, audit reason 'apple_deleted_after_use' giúp truy
                # nguồn khi user complaint email không nhận được forward.
                if not in_apple and db_status == "used_for_chatgpt":
                    self._pool_repo.update_email_status(
                        row["email"],
                        status="disabled",
                        last_sync_at=now,
                    )
                    self._audit_repo.write(
                        event_type="email_delete",
                        apple_id=apple_id,
                        payload={
                            "email": row["email"],
                            "hme_id": hme_id,
                            "reason": "apple_deleted_after_use",
                            "previous_status": "used_for_chatgpt",
                        },
                    )
                    diff.db_marked_deleted += 1
                    continue

                # Nhánh 5: apple active + db {deactivated, revoked}
                if (
                    in_apple
                    and apple_active is True
                    and db_status in ("deactivated", "revoked")
                ):
                    self._pool_repo.update_email_status(
                        row["email"],
                        status="created",
                        reactivated_at=now,
                        last_sync_at=now,
                    )
                    self._audit_repo.write(
                        event_type="email_reactivate",
                        apple_id=apple_id,
                        payload={
                            "email": row["email"],
                            "hme_id": hme_id,
                            "reason": "external_change",
                        },
                    )
                    diff.db_marked_reactivated += 1
                    continue

                # Không thuộc nhánh nào — unchanged.
                diff.unchanged += 1

        return diff

    # =====================================================================
    # Internals — single + bulk dispatcher
    # =====================================================================

    async def _single_action(
        self,
        email: str,
        *,
        action: str,
        dry_run: bool,
        extra: dict | None = None,
    ) -> LifecycleResult:
        """Single-email action (deactivate/reactivate/delete/update_meta).

        Flow:
            1. Lookup row → not found → failed.
            2. Check terminal: status NOT in acceptable → raise
               TerminalStatusError (R9.13, R9.14, Property 18).
            3. dry_run=True → return list email "sẽ tác động" (R9.18).
            4. Lookup profile → session_expired/deleted → skipped (R9.3).
            5. Extract bundle → SessionExtractError → fail.
            6. Call API → success → UPDATE DB + audit (1 tx).
               HmeNotFoundError → handle theo action (deactivate/delete →
               UPDATE deleted; reactivate/update_meta → audit fail).
               HmeQuotaError → mark_limited + audit fail.
               HmeAuthError → mark_session_expired + audit fail.
        """
        row = self._pool_repo.get_email(email)
        if row is None:
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[],
                remaining=[],
                failed=[
                    {
                        "email": email,
                        "reason": "email_not_found",
                        "error": f"email không tồn tại: {email}",
                    }
                ],
                dry_run=dry_run,
            )

        current_status = row["status"]
        if current_status not in _ACCEPTABLE_STATUSES[action]:
            # Property 18: precondition fail → raise, KHÔNG gọi API, KHÔNG
            # UPDATE DB.
            raise TerminalStatusError(
                email=email,
                current_status=current_status,
                action=action,
            )

        if dry_run:
            # R9.18: trả list email "sẽ tác động", KHÔNG gọi API/UPDATE/audit.
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[
                    {
                        "email": email,
                        "apple_id": row["apple_id"],
                        "hme_id": row.get("hme_id"),
                        "label": row.get("label"),
                        "created_at": row.get("created_at"),
                    }
                ],
                remaining=[],
                failed=[],
                dry_run=True,
            )

        apple_id = row["apple_id"]
        # Lookup profile status (R9.3)
        account = self._pool_repo.get(apple_id)
        if account is None or account.status in (
            "session_expired",
            "deleted",
        ):
            self._audit_repo.write(
                event_type=_AUDIT_FAIL_BY_ACTION[action],
                apple_id=apple_id,
                payload={
                    "email": email,
                    "reason": "profile_unavailable",
                    "profile_status": account.status if account else "missing",
                },
            )
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[
                    {"email": email, "reason": "profile_unavailable"}
                ],
                remaining=[],
                failed=[],
                dry_run=False,
            )

        # Extract bundle
        try:
            bundle = await self._extract_fn(
                profile_dir=account.profile_dir,
                apple_id=apple_id,
                audit_repo=self._audit_repo,
                proxy=None,
                log=self._log,
            )
        except SessionExtractError as exc:
            self._audit_repo.write(
                event_type=_AUDIT_FAIL_BY_ACTION[action],
                apple_id=apple_id,
                payload={
                    "email": email,
                    "reason": "session_extract_failed",
                    "error": str(exc),
                },
            )
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[],
                remaining=[],
                failed=[
                    {
                        "email": email,
                        "reason": "session_extract_failed",
                        "error": str(exc),
                    }
                ],
                dry_run=False,
            )

        client = self._client_factory(bundle)
        try:
            await self._dispatch_api_call(
                client, action=action, row=row, extra=extra
            )
        except HmeNotFoundError as exc:
            return await self._handle_not_found(
                email=email,
                apple_id=apple_id,
                hme_id=row.get("hme_id"),
                action=action,
                error=str(exc),
            )
        except HmeQuotaError as exc:
            self._pool.mark_limited(apple_id, reason=f"HmeQuotaError: {exc}")
            self._audit_repo.write(
                event_type=_AUDIT_FAIL_BY_ACTION[action],
                apple_id=apple_id,
                payload={
                    "email": email,
                    "reason": "rate_limit",
                    "error": str(exc),
                },
            )
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[],
                remaining=[email],
                failed=[],
                dry_run=False,
            )
        except HmeAuthError as exc:
            self._pool.mark_session_expired(
                apple_id, reason=f"HmeAuthError: {exc}"
            )
            self._audit_repo.write(
                event_type=_AUDIT_FAIL_BY_ACTION[action],
                apple_id=apple_id,
                payload={
                    "email": email,
                    "reason": "session_expired",
                    "error": str(exc),
                },
            )
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[],
                remaining=[email],
                failed=[],
                dry_run=False,
            )
        except HmeClientError as exc:
            self._audit_repo.write(
                event_type=_AUDIT_FAIL_BY_ACTION[action],
                apple_id=apple_id,
                payload={
                    "email": email,
                    "reason": "client_error",
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return LifecycleResult(
                requested=1,
                succeeded=0,
                skipped=[],
                remaining=[],
                failed=[
                    {
                        "email": email,
                        "reason": "client_error",
                        "error": str(exc),
                    }
                ],
                dry_run=False,
            )
        finally:
            await client.aclose()

        # API success → UPDATE DB + audit (1 tx).
        self._apply_success_update(
            email=email,
            apple_id=apple_id,
            hme_id=row.get("hme_id"),
            action=action,
            extra=extra,
            row=row,
        )
        return LifecycleResult(
            requested=1,
            succeeded=1,
            skipped=[],
            remaining=[],
            failed=[],
            dry_run=False,
        )

    async def _bulk_action(
        self,
        emails: list[str],
        *,
        action: str,
        dry_run: bool,
        extras_by_email: dict[str, dict] | None = None,
    ) -> LifecycleResult:
        """Bulk action — group by apple_id chủ + reuse SessionBundle in-memory
        + delay random ``[delay_min, delay_max]``s giữa cặp request kế tiếp
        trong cùng group (R9.2, R9.7, R9.17).

        Skip + remaining + failed semantics:
            - skipped: precondition fail (terminal status, profile unavailable)
              hoặc dry_run preview rows.
            - remaining: emails chưa xử lý vì group dừng giữa chừng (quota/auth).
            - failed: lỗi runtime (not_found, db_persist).
        """
        if not emails:
            return LifecycleResult(
                requested=0,
                succeeded=0,
                skipped=[],
                remaining=[],
                failed=[],
                dry_run=dry_run,
            )

        # Group emails theo apple_id chủ (preserve order trong từng group).
        groups: dict[str, list[dict]] = {}
        order_apple_ids: list[str] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        remaining: list[str] = []
        succeeded_count = 0
        requested = len(emails)

        for email in emails:
            row = self._pool_repo.get_email(email)
            if row is None:
                failed.append(
                    {
                        "email": email,
                        "reason": "email_not_found",
                        "error": f"email không tồn tại: {email}",
                    }
                )
                continue
            current_status = row["status"]
            if current_status not in _ACCEPTABLE_STATUSES[action]:
                # Bulk: precondition fail → skip email + audit fail (R9
                # bulk skip semantics). KHÔNG raise (R9.18 dry_run preview
                # vẫn có row, nhưng terminal preview cũng bị skip).
                skipped.append(
                    {
                        "email": email,
                        "reason": "terminal_status",
                        "current_status": current_status,
                    }
                )
                if not dry_run:
                    self._audit_repo.write(
                        event_type=_AUDIT_FAIL_BY_ACTION[action],
                        apple_id=row["apple_id"],
                        payload={
                            "email": email,
                            "reason": "terminal_status",
                            "current_status": current_status,
                        },
                    )
                continue

            apple_id = row["apple_id"]
            if apple_id not in groups:
                groups[apple_id] = []
                order_apple_ids.append(apple_id)
            groups[apple_id].append(row)

        # Dry_run preview: chỉ trả list email "sẽ tác động", KHÔNG gọi API
        # / UPDATE / audit. Skipped đã chứa các email terminal.
        if dry_run:
            preview_skipped: list[dict] = list(skipped)
            for apple_id in order_apple_ids:
                for row in groups[apple_id]:
                    preview_skipped.append(
                        {
                            "email": row["email"],
                            "apple_id": row["apple_id"],
                            "hme_id": row.get("hme_id"),
                            "label": row.get("label"),
                            "created_at": row.get("created_at"),
                        }
                    )
            return LifecycleResult(
                requested=requested,
                succeeded=0,
                skipped=preview_skipped,
                remaining=[],
                failed=failed,
                dry_run=True,
            )

        # Process từng group: extract bundle 1 lần + loop emails với delay.
        for apple_id in order_apple_ids:
            group_rows = groups[apple_id]
            account = self._pool_repo.get(apple_id)
            if account is None or account.status in (
                "session_expired",
                "deleted",
            ):
                # R9.3: skip cả group + audit fail từng email
                for row in group_rows:
                    skipped.append(
                        {
                            "email": row["email"],
                            "reason": "profile_unavailable",
                        }
                    )
                    self._audit_repo.write(
                        event_type=_AUDIT_FAIL_BY_ACTION[action],
                        apple_id=apple_id,
                        payload={
                            "email": row["email"],
                            "reason": "profile_unavailable",
                            "profile_status": account.status
                            if account
                            else "missing",
                        },
                    )
                continue

            try:
                bundle = await self._extract_fn(
                    profile_dir=account.profile_dir,
                    apple_id=apple_id,
                    audit_repo=self._audit_repo,
                    proxy=None,
                    log=self._log,
                )
            except SessionExtractError as exc:
                for row in group_rows:
                    failed.append(
                        {
                            "email": row["email"],
                            "reason": "session_extract_failed",
                            "error": str(exc),
                        }
                    )
                continue

            client = self._client_factory(bundle)
            group_aborted = False
            try:
                for idx, row in enumerate(group_rows):
                    if group_aborted:
                        remaining.append(row["email"])
                        continue
                    email = row["email"]
                    extra = (
                        extras_by_email.get(email)
                        if extras_by_email
                        else None
                    )
                    # Apply random delay giữa cặp request kế tiếp trong group
                    # (R9.7). Trước request[1], request[2], ... — KHÔNG
                    # trước request[0].
                    if idx > 0:
                        delay = random.uniform(
                            self._delay_range[0], self._delay_range[1]
                        )
                        await self._sleep(delay)

                    try:
                        await self._dispatch_api_call(
                            client, action=action, row=row, extra=extra
                        )
                    except HmeNotFoundError as exc:
                        result = await self._handle_not_found(
                            email=email,
                            apple_id=apple_id,
                            hme_id=row.get("hme_id"),
                            action=action,
                            error=str(exc),
                        )
                        if result.succeeded:
                            succeeded_count += 1
                        elif result.failed:
                            failed.extend(result.failed)
                        else:
                            failed.append(
                                {
                                    "email": email,
                                    "reason": "not_found_remote",
                                    "error": str(exc),
                                }
                            )
                        continue
                    except HmeQuotaError as exc:
                        self._pool.mark_limited(
                            apple_id, reason=f"HmeQuotaError: {exc}"
                        )
                        self._audit_repo.write(
                            event_type=_AUDIT_FAIL_BY_ACTION[action],
                            apple_id=apple_id,
                            payload={
                                "email": email,
                                "reason": "rate_limit",
                                "error": str(exc),
                            },
                        )
                        remaining.append(email)
                        group_aborted = True
                        continue
                    except HmeAuthError as exc:
                        self._pool.mark_session_expired(
                            apple_id, reason=f"HmeAuthError: {exc}"
                        )
                        self._audit_repo.write(
                            event_type=_AUDIT_FAIL_BY_ACTION[action],
                            apple_id=apple_id,
                            payload={
                                "email": email,
                                "reason": "session_expired",
                                "error": str(exc),
                            },
                        )
                        remaining.append(email)
                        group_aborted = True
                        continue
                    except HmeClientError as exc:
                        self._audit_repo.write(
                            event_type=_AUDIT_FAIL_BY_ACTION[action],
                            apple_id=apple_id,
                            payload={
                                "email": email,
                                "reason": "client_error",
                                "error_class": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        failed.append(
                            {
                                "email": email,
                                "reason": "client_error",
                                "error": str(exc),
                            }
                        )
                        continue

                    self._apply_success_update(
                        email=email,
                        apple_id=apple_id,
                        hme_id=row.get("hme_id"),
                        action=action,
                        extra=extra,
                        row=row,
                    )
                    succeeded_count += 1
            finally:
                await client.aclose()

        return LifecycleResult(
            requested=requested,
            succeeded=succeeded_count,
            skipped=skipped,
            remaining=remaining,
            failed=failed,
            dry_run=False,
        )

    # =====================================================================
    # Helper — API dispatch + DB success update + 404 handler
    # =====================================================================

    async def _dispatch_api_call(
        self,
        client: HmeClient,
        *,
        action: str,
        row: dict,
        extra: dict | None,
    ) -> None:
        """Gọi đúng method trên HmeClient theo action."""
        hme_id = row.get("hme_id")
        if not hme_id:
            raise HmeClientError(
                f"row {row.get('email')} thiếu hme_id — không thể gọi API {action}"
            )
        if action == "deactivate":
            await client.deactivate(hme_id)
        elif action == "reactivate":
            await client.reactivate(hme_id)
        elif action == "delete":
            await client.delete(hme_id)
        elif action == "update_meta":
            label = (extra or {}).get("label") or row.get("label") or ""
            note = (extra or {}).get("note")
            await client.update_meta(hme_id, label, note)
        else:  # pragma: no cover — guarded by enum
            raise ValueError(f"action không hợp lệ: {action}")

    def _apply_success_update(
        self,
        *,
        email: str,
        apple_id: str,
        hme_id: str | None,
        action: str,
        extra: dict | None,
        row: dict,
    ) -> None:
        """UPDATE DB + audit cho success path (1 tx, R6.3)."""
        engine = self._pool_repo.engine
        now = _utc_now()
        with engine.transaction() as _conn:
            if action == "deactivate":
                self._pool_repo.update_email_status(
                    email, status="deactivated", deactivated_at=now
                )
                payload: dict = {
                    "apple_id": apple_id,
                    "email": email,
                    "hme_id": hme_id,
                }
            elif action == "reactivate":
                self._pool_repo.update_email_status(
                    email, status="created", reactivated_at=now
                )
                payload = {
                    "apple_id": apple_id,
                    "email": email,
                    "hme_id": hme_id,
                }
            elif action == "delete":
                self._pool_repo.update_email_status(
                    email, status="deleted", deleted_at=now
                )
                payload = {
                    "apple_id": apple_id,
                    "email": email,
                    "hme_id": hme_id,
                }
            elif action == "update_meta":
                # Không đổi status — UPDATE label/note bằng status hiện tại.
                new_label = (extra or {}).get("label")
                new_note = (extra or {}).get("note")
                self._pool_repo.update_email_status(
                    email,
                    status=row["status"],  # giữ nguyên
                    label=new_label,
                    note=new_note,
                )
                payload = {
                    "apple_id": apple_id,
                    "email": email,
                    "label_old": row.get("label"),
                    "label_new": new_label,
                    "note_old": row.get("note"),
                    "note_new": new_note,
                }
            else:  # pragma: no cover
                raise ValueError(f"action không hợp lệ: {action}")
            self._audit_repo.write(
                event_type=_AUDIT_SUCCESS_BY_ACTION[action],
                apple_id=apple_id,
                payload=payload,
            )

    async def _handle_not_found(
        self,
        *,
        email: str,
        apple_id: str,
        hme_id: str | None,
        action: str,
        error: str,
    ) -> LifecycleResult:
        """Xử lý 404 từ Apple (R9.6, R9.15).

        - deactivate / delete: vẫn UPDATE status='deleted' + audit fail
          với reason 'not_found_remote' (deactivate) / 'already_deleted_remote'
          (delete). Cả 2 SHALL count ``succeeded=1`` vì state DB đã match
          Apple-side (intent deactivate = "ẩn"; Apple đã xoá hẳn → coi như
          ẩn thành công). Trước fix A9, deactivate 404 trả ``succeeded=0
          failed=1`` khiến UI hiện confusing "5 deactivated, 1 failed" dù
          tất cả 5 đều đã offline Apple-side.
        - reactivate / update_meta: audit fail với reason 'terminal_remote',
          KHÔNG UPDATE DB (Apple-side đã không còn → không thể reactivate /
          update_meta được nữa) → ``succeeded=0 failed=1``.
        """
        engine = self._pool_repo.engine
        now = _utc_now()
        if action in ("deactivate", "delete"):
            reason = (
                "not_found_remote"
                if action == "deactivate"
                else "already_deleted_remote"
            )
            with engine.transaction() as _conn:
                self._pool_repo.update_email_status(
                    email, status="deleted", deleted_at=now
                )
                self._audit_repo.write(
                    event_type=_AUDIT_FAIL_BY_ACTION[action],
                    apple_id=apple_id,
                    payload={
                        "email": email,
                        "hme_id": hme_id,
                        "reason": reason,
                        "error": error,
                    },
                )
            return LifecycleResult(
                requested=1,
                succeeded=1,
                skipped=[],
                remaining=[],
                failed=[],
                dry_run=False,
            )

        # reactivate / update_meta: audit fail, không UPDATE DB.
        self._audit_repo.write(
            event_type=_AUDIT_FAIL_BY_ACTION[action],
            apple_id=apple_id,
            payload={
                "email": email,
                "hme_id": hme_id,
                "reason": "terminal_remote",
                "error": error,
            },
        )
        return LifecycleResult(
            requested=1,
            succeeded=0,
            skipped=[],
            remaining=[],
            failed=[
                {
                    "email": email,
                    "reason": "terminal_remote",
                    "error": error,
                }
            ],
            dry_run=False,
        )
