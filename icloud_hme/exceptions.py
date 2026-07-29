"""Exception hierarchy cho icloud-hme-pool feature.

Refs: design.md §Components / Exceptions, tasks.md task 5.

Hierarchy:
    IcloudError (base)
        IcloudPoolError
        BootstrapError
        SessionExtractError(apple_id, missing_fields, reason=None)
        TerminalStatusError(email, current_status, action)
        HmeClientError
            HmeQuotaError
            HmeAuthError
            HmeReserveTaken
            HmeNotFoundError
            HmeTransientError
        ProfileLockError(apple_id, mode, reason)

KHÔNG re-export ngoài module này để tránh circular import. `icloud_hme/__init__.py`
sẽ re-export tên public ở task 21 (sau khi mọi service ổn định).
"""

from __future__ import annotations


class IcloudError(Exception):
    """Base cho mọi exception thuộc icloud_hme module."""


class IcloudPoolError(IcloudError):
    """Pool_Manager: pick / mark state thất bại (vd pool_pick_locked)."""


class BootstrapError(IcloudError):
    """Bootstrap_Flow: 2FA / login flow / cookie verify thất bại."""


class SessionExtractError(IcloudError):
    """Session_Extractor extraction fail (refactor B — cookies-only).

    ``missing_fields`` thường = ``['cookies']`` khi cookies dict empty hoặc
    thiếu marker login (X-APPLE-WEBAUTH-USER / -TOKEN / -PCS-Mail). Có thể
    rỗng khi reason='profile_locked_by_bootstrap' (lock conflict trước
    khi extract).

    Trước refactor B, ``missing_fields`` còn chứa 'dsid', 'maildomainws_host'
    nhưng các field này đã bị bỏ vì Apple HME API không enforce.
    """

    def __init__(
        self,
        apple_id: str,
        missing_fields: list[str],
        reason: str | None = None,
    ) -> None:
        self.apple_id = apple_id
        self.missing_fields = list(missing_fields)
        self.reason = reason
        msg = (
            f"session extract failed apple_id={apple_id} "
            f"missing={self.missing_fields}"
        )
        if reason:
            msg += f" reason={reason}"
        super().__init__(msg)


class TerminalStatusError(IcloudError):
    """Email đang ở terminal status (deleted/used_for_chatgpt) — không hợp lệ cho action."""

    def __init__(self, email: str, current_status: str, action: str) -> None:
        self.email = email
        self.current_status = current_status
        self.action = action
        super().__init__(
            f"email={email} status={current_status} cannot {action}"
        )


class HmeClientError(IcloudError):
    """Base cho mọi lỗi trả từ icloud HME REST client."""


class HmeQuotaError(HmeClientError):
    """Quota account hết (HTTP 429 hoặc body marker rate limit / quota / limit)."""


class HmeAuthError(HmeClientError):
    """Session expired / unauthorized (HTTP 401/421/440 hoặc auth marker)."""


class HmeReserveTaken(HmeClientError):
    """reserve: email đã bị account khác chiếm (already / taken / unavailable)."""


class HmeNotFoundError(HmeClientError):
    """deactivate / reactivate / delete / update_meta gặp HTTP 404."""


class HmeTransientError(HmeClientError):
    """Timeout / network / 5xx — caller có thể retry."""


class ProfileLockError(IcloudError):
    """Profile_Lock acquire fail (write+write block, write blocks read, ...)."""

    def __init__(self, apple_id: str, mode: str, reason: str) -> None:
        self.apple_id = apple_id
        self.mode = mode
        self.reason = reason
        super().__init__(
            f"profile lock apple_id={apple_id} mode={mode} reason={reason}"
        )


class AddProfileError(IcloudError):
    """Add_Profile_Flow web extension lỗi (R14).

    reason ∈ {
        'add_profile_in_progress',     # R14.10 — đã có session active
        'apple_id_not_extractable',    # R14.4  — không parse được apple_id từ cookies
        'apple_id_mismatch',           # A17    — user hint != auto-extract apple_id
        'cookies_not_ready',           # R14.5  — thiếu X-APPLE-WEBAUTH-PCS-Mail / -USER
        'apple_id_already_exists',     # R14.6  — apple_id đã có row trong DB
        'move_failed',                 # R14.11 — rename profile_dir tạm fail (file lock)
        'session_not_found',           # session_id không tồn tại / đã kết thúc
        'invalid_state',               # gọi save từ state ≠ recording (race / double-click)
        'process_crashed',             # R14.12 — orphan profile_dir tạm còn sót
        'unexpected',                  # exception không xử lý được — phải log + raise tiếp
    }
    """

    def __init__(self, reason: str, message: str, *, session_id: str | None = None) -> None:
        self.reason = reason
        self.session_id = session_id
        super().__init__(message)


class OpenProfileError(IcloudError):
    """Open_Profile_Flow web + CLI extension lỗi (R15).

    reason ∈ {
        'profile_not_found',           # R15.2  — apple_id không tồn tại / status='deleted' / profile_dir IS NULL
        'profile_locked',              # R15.3  — Profile_Lock write conflict (Bootstrap/Recorder/Open khác đang giữ)
        'open_profile_in_progress',    # R15.4  — đã có Open_Profile_Session non-terminal khác (single-instance)
        'cookies_not_ready',           # R15.7  — verify cookies fail (recoverable: revert SAVING → OPEN, giữ browser)
        'session_not_found',           # session_id không tồn tại trong active lẫn FIFO terminal cache
        'invalid_state',               # save/close gọi từ state ≠ hợp lệ (vd save từ closed)
        'unexpected',                  # exception không xử lý được — phải log + raise tiếp
    }
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        session_id: str | None = None,
        apple_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.session_id = session_id
        self.apple_id = apple_id
        super().__init__(message)
