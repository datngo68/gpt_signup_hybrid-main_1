"""Profile_Checker — verify session validity của 1 profile iCloud (R4).

Refs:
  - .kiro/specs/icloud-hme-pool/design.md §6 (Profile_Checker class) + sequence diagram (line 188-235).
  - .kiro/specs/icloud-hme-pool/requirements.md R4.1-R4.9, R12.3.
  - Property 7: status mapping từ probe outcome.

Class ``ProfileChecker`` orchestrate flow:
    1. Đọc profile_dir từ ``IcloudPoolRepository`` (R4 + R5.7 — profile đã delete có
       ``profile_dir IS NULL``).
    2. profile_dir không tồn tại → ``CheckResult(status='missing_profile')`` (R4.6).
    3. ``extract_session_bundle`` (R12.3) — fail → ``CheckResult(status='session_expired')``
       + ``auto_mark`` → ``pool.mark_session_expired`` (R4.4 mở rộng).
    4. ``HmeClient(bundle).list()`` read-only probe (R4.8) → map exception sang
       ``CheckResult.status`` theo bảng:
         - ``HmeAuthError`` → ``'session_expired'`` + auto_mark → ``mark_session_expired`` (R4.4).
         - ``HmeQuotaError`` → ``'limited'`` + auto_mark → ``mark_limited`` (R4.5).
         - ``HmeTransientError`` → ``'error'``.
         - 200 OK → ``'active'``, ``hme_count_remote = len(items)`` (R4.3).
    5. KHÔNG gọi ``generate``/``reserve`` (R4.8). KHÔNG headed (R4.9).

Dependency injection:
    Constructor accept callable ``extract_bundle`` + ``client_factory`` để tách rời
    Camoufox / httpx infrastructure. Default factories raise ``NotImplementedError``
    fail-fast nếu caller chưa wire upstream (task 8/9). Tests inject mock thuần.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .exceptions import (
    HmeAuthError,
    HmeClientError,
    HmeQuotaError,
    HmeTransientError,
    SessionExtractError,
)
from .models import AppleAccount, CheckResult, SessionBundle


# --- Protocol cho dependencies (duck-typing, tránh import vòng) ----------


class _PoolManagerProto(Protocol):
    """Subset Pool_Manager interface mà Profile_Checker cần."""

    def mark_session_expired(self, apple_id: str, *, reason: str) -> None: ...

    def mark_limited(self, apple_id: str, *, reason: str) -> None: ...


class _PoolRepoProto(Protocol):
    """Subset IcloudPoolRepository interface."""

    def get(self, apple_id: str) -> AppleAccount | None: ...

    def list_all(self) -> list[AppleAccount]: ...


class _AuditRepoProto(Protocol):
    """Subset AuditLogRepository interface — Profile_Checker hiện không
    tự ghi audit (delegate cho ``extract_session_bundle`` qua R12.5).
    Inject để giữ chỗ cho future event types khi spec mở rộng.
    """


class _HmeClientProto(Protocol):
    """Subset HmeClient interface — chỉ cần list() (R4.8) + aclose."""

    async def list(self) -> list: ...  # noqa: A003 — match design name

    async def aclose(self) -> None: ...


# Type aliases cho injectable callables.
_ExtractBundleFn = Callable[..., Awaitable[SessionBundle]]
_ClientFactoryFn = Callable[[SessionBundle], _HmeClientProto]


def _default_extract_bundle_factory() -> _ExtractBundleFn:
    """Default: lazy import ``session.extract_session_bundle`` (task 8).

    Fail-fast khi task 8 chưa hoàn tất (function chưa tồn tại) — caller phải
    inject mock hoặc đợi task 8 wire-up xong.
    """

    async def _delegate(**kwargs):  # noqa: ANN001 — pass-through kwargs
        try:
            from .session import extract_session_bundle  # type: ignore[attr-defined]
        except ImportError as exc:
            raise NotImplementedError(
                "icloud_hme.session.extract_session_bundle chưa implement "
                "(task 8). Profile_Checker caller phải inject extract_bundle "
                "qua constructor."
            ) from exc
        return await extract_session_bundle(**kwargs)

    return _delegate


def _default_client_factory_factory(log) -> _ClientFactoryFn:
    """Default: tạo ``HmeClient(bundle, log=log)`` (task 9 refactored client).

    Fail-fast khi task 9 chưa hoàn tất (HmeClient signature cũ nhận Page,
    không nhận SessionBundle).
    """

    def _factory(bundle: SessionBundle) -> _HmeClientProto:
        try:
            from .client import HmeClient  # type: ignore[attr-defined]
        except ImportError as exc:
            raise NotImplementedError(
                "icloud_hme.client.HmeClient chưa refactor (task 9). "
                "Profile_Checker caller phải inject client_factory qua constructor."
            ) from exc
        try:
            return HmeClient(bundle, log=log)  # type: ignore[call-arg]
        except TypeError as exc:
            raise NotImplementedError(
                "HmeClient(SessionBundle, log=...) signature chưa sẵn sàng "
                "(task 9 chưa refactor sang httpx async + SessionBundle). "
                "Profile_Checker caller phải inject client_factory."
            ) from exc

    return _factory


# --- ProfileChecker --------------------------------------------------------


class ProfileChecker:
    """Probe session validity 1 profile iCloud qua API read-only.

    Service layer — KHÔNG gọi DB/Camoufox trực tiếp. Phụ thuộc:
      - ``pool`` (Pool_Manager): ``mark_session_expired``/``mark_limited``.
      - ``pool_repo`` (IcloudPoolRepository): ``get(apple_id)``/``list_all()``.
      - ``audit_repo`` (AuditLogRepository): giữ chỗ, hiện chưa ghi event riêng.
      - ``extract_bundle`` (callable): ``extract_session_bundle`` (task 8).
      - ``client_factory`` (callable): factory tạo ``HmeClient(bundle)`` (task 9).

    KHÔNG retry — Profile_Checker là probe đơn giản, fail-fast (theo bảng
    "Retry semantics" trong design.md line 2402).
    """

    def __init__(
        self,
        pool: _PoolManagerProto,
        pool_repo: _PoolRepoProto,
        audit_repo: _AuditRepoProto,
        *,
        log,
        extract_bundle: _ExtractBundleFn | None = None,
        client_factory: _ClientFactoryFn | None = None,
    ) -> None:
        self._pool = pool
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._log = log
        self._extract_bundle: _ExtractBundleFn = (
            extract_bundle if extract_bundle is not None else _default_extract_bundle_factory()
        )
        self._client_factory: _ClientFactoryFn = (
            client_factory if client_factory is not None else _default_client_factory_factory(log)
        )

    async def check_one(
        self,
        apple_id: str,
        *,
        auto_mark: bool = False,
        proxy: str | None = None,
    ) -> CheckResult:
        """Check 1 profile + map outcome → CheckResult (R4.1, R4.3-R4.6).

        Args:
            apple_id: Apple ID cần check.
            auto_mark: True → side-effect ``pool.mark_*`` khi probe phát hiện
                session_expired / limited (R4.4, R4.5).
            proxy: optional proxy URL forward xuống ``extract_bundle``.

        Returns:
            ``CheckResult`` luôn — KHÔNG raise (mọi exception capture vào field
            ``error`` + ``error_class``). Fail-fast chỉ apply ở dependency
            invariant (vd ``pool_repo.get`` raise — bubble lên).
        """
        account = self._pool_repo.get(apple_id)
        if account is None:
            return CheckResult(
                apple_id=apple_id,
                ok=False,
                status="missing_profile",
                hme_count_remote=None,
                hme_count_local=0,
                error=f"apple_id không tồn tại trong pool: {apple_id}",
                error_class="NotInDB",
            )

        hme_count_local = int(account.hme_count)

        # R4.6: profile_dir không tồn tại trên disk → missing_profile.
        # profile_dir IS NULL (sau Pool_Manager.delete_profile) cũng coi là missing.
        profile_dir = account.profile_dir
        if profile_dir is None or not Path(profile_dir).exists():
            return CheckResult(
                apple_id=apple_id,
                ok=False,
                status="missing_profile",
                hme_count_remote=None,
                hme_count_local=hme_count_local,
                error=f"profile_dir không tồn tại: {profile_dir}",
                error_class="MissingProfile",
            )

        # extract_session_bundle (R12.3 — read-lock + Camoufox headless ngắn).
        # extract_bundle tự ghi audit `session_extract`/`session_extract_fail` (R12.5).
        try:
            bundle = await self._extract_bundle(
                profile_dir=Path(profile_dir),
                apple_id=apple_id,
                audit_repo=self._audit_repo,
                proxy=proxy,
                log=self._log,
            )
        except SessionExtractError as exc:
            if auto_mark:
                self._pool.mark_session_expired(
                    apple_id, reason=f"session_extract_failed: {exc}"
                )
            return CheckResult(
                apple_id=apple_id,
                ok=False,
                status="session_expired",
                hme_count_remote=None,
                hme_count_local=hme_count_local,
                error=str(exc),
                error_class="SessionExtractError",
            )

        # R4.8: GET /v2/hme/list (read-only probe). Map exception per bảng outcome.
        client = self._client_factory(bundle)
        try:
            try:
                items = await client.list()
            except HmeAuthError as exc:
                if auto_mark:
                    self._pool.mark_session_expired(
                        apple_id, reason=f"hme_auth_error: {exc}"
                    )
                return CheckResult(
                    apple_id=apple_id,
                    ok=False,
                    status="session_expired",
                    hme_count_remote=None,
                    hme_count_local=hme_count_local,
                    error=str(exc),
                    error_class="HmeAuthError",
                )
            except HmeQuotaError as exc:
                if auto_mark:
                    self._pool.mark_limited(
                        apple_id, reason=f"hme_quota_error: {exc}"
                    )
                return CheckResult(
                    apple_id=apple_id,
                    ok=False,
                    status="limited",
                    hme_count_remote=None,
                    hme_count_local=hme_count_local,
                    error=str(exc),
                    error_class="HmeQuotaError",
                )
            except HmeTransientError as exc:
                # Transient (5xx / timeout / network) — không auto_mark, không fatal.
                return CheckResult(
                    apple_id=apple_id,
                    ok=False,
                    status="error",
                    hme_count_remote=None,
                    hme_count_local=hme_count_local,
                    error=str(exc),
                    error_class="HmeTransientError",
                )
            except HmeClientError as exc:
                # Lỗi HmeClient khác (HmeReserveTaken / HmeNotFoundError) không
                # nên xảy ra với probe list-only — bắt fallback sang status='error'.
                return CheckResult(
                    apple_id=apple_id,
                    ok=False,
                    status="error",
                    hme_count_remote=None,
                    hme_count_local=hme_count_local,
                    error=str(exc),
                    error_class=type(exc).__name__,
                )
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 — close fail không che lỗi probe
                    pass

        # R4.3: 200 success → status='active'.
        return CheckResult(
            apple_id=apple_id,
            ok=True,
            status="active",
            hme_count_remote=len(items),
            hme_count_local=hme_count_local,
            error=None,
            error_class=None,
        )

    async def check_all(
        self,
        *,
        auto_mark: bool = False,
        proxy: str | None = None,
    ) -> list[CheckResult]:
        """Check tuần tự, chỉ profile có status ∈ {active, limited} (R4.2).

        Returns:
            list[CheckResult] — order theo ``apple_id`` ASC như ``pool_repo.list_all()``.
        """
        accounts = self._pool_repo.list_all()
        eligible = [a for a in accounts if a.status in ("active", "limited")]
        results: list[CheckResult] = []
        for account in eligible:
            try:
                self._log(f"checking {account.apple_id} (status={account.status})...")
            except Exception:  # noqa: BLE001 — log không được phép phá flow
                pass
            result = await self.check_one(
                account.apple_id, auto_mark=auto_mark, proxy=proxy
            )
            results.append(result)
        return results
