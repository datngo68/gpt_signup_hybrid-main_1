"""Mail providers cho OTP polling.

5 backends + 1 wrapper:
    - WorkerMailProvider:          Cloudflare Worker logs API (icloud-cf-mail style).
    - OutlookMailProvider:         Microsoft Graph API qua refresh_token (combo Outlook).
    - DongVanFBOutlookProvider:    tools.dongvanfb.net API qua refresh_token (combo Outlook).
    - GmailAdvancedProvider:       checkotpgmail.live API.
    - ChinaICloudProvider:         icloudapi.xyz mailbox viewer (HTML response).
    - OutlookCascadeProvider:      Wrapper — DongVanFB trước, fallback Microsoft Graph
                                   khi DongVanFB API down hoặc poll timeout. Sticky
                                   bypass khi DongVanFB fail trong cùng process.

Factory `build_provider_outlook` mặc định trả OutlookCascadeProvider (auto-fallback);
muốn fix-provider thì dùng `build_provider_dongvanfb` trực tiếp.

Mỗi provider có method:
    async def poll_otp(*, recipient, started_at, timeout_seconds, poll_interval_seconds, log) -> str
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from db.repositories import ComboRepository


# UA browser: Cloudflare (Bot Fight Mode/WAF, error 1010) chặn UA httpx/urllib.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_OTP_REGEX = re.compile(
    r"(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code)"
    r"[^0-9]{0,40}(\d{6})"
    r"|(?<!\d)(\d{6})(?!\d)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_dt(value: Any) -> datetime | None:
    """Parse datetime từ nhiều format khác nhau."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S GMT", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _extract_otp(subject: str, body: str) -> str | None:
    """Tìm code 6 chữ số trong subject + body."""
    cleaned = re.sub(r"<[^>]*>", " ", f"{subject}\n{body}")
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    match = _OTP_REGEX.search(cleaned)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _sort_messages_newest_first(messages: list[dict[str, Any]]) -> None:
    """Sort in-place mới→cũ theo date/receivedAt/created_at.

    Nếu KHÔNG message nào có date hợp lệ (iCloud worker đôi khi không trả) →
    giữ nguyên thứ tự gốc của API (không đảo lung tung).
    """
    has_any_date = any(
        _parse_dt(m.get("date") or m.get("receivedAt") or m.get("created_at"))
        for m in messages
    )
    if has_any_date:
        messages.sort(
            key=lambda m: (
                _parse_dt(m.get("date") or m.get("receivedAt") or m.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )


def _is_openai_sender(sender: str) -> bool:
    """Filter mail từ OpenAI để tránh nhặt nhầm OTP của dịch vụ khác."""
    s = (sender or "").lower()
    return any(d in s for d in ("openai.com", "auth.openai.com", "noreply@openai", "tm.openai.com"))


class MailProvider(Protocol):
    """Interface chung."""

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        ...


# ─────────────────────────────────────────────────────────────────────
# Worker provider (icloud-cf-mail style)
# ─────────────────────────────────────────────────────────────────────


# NOTE: Worker provider KHÔNG còn lọc OTP theo thời gian (`started_at`). Recipient
# là +alias duy nhất cho mỗi phiên đăng ký nên mailbox chỉ chứa mã của phiên hiện
# tại; mã được sort mới→cũ và caller dedup qua tried_codes. Lọc theo `date` (HME
# relay lệch giờ, poll_started reset sau resend) trước đây gây loại nhầm mã hợp lệ.
# Param `started_at` vẫn giữ trong signature để đồng nhất interface MailProvider
# (các provider inbox-dùng-lại như Outlook/DongVanFB/Gmail vẫn cần lọc thời gian).

# Số mã OTP tối đa lấy về trong 1 lần poll_all_codes (mới→cũ). Đủ để bắt mail-delay
# trong cùng phiên (vài lần resend) mà không thử dồn mã cũ đã bị vô hiệu.
_WORKER_MAX_CODES = 5


class WorkerMailProvider:
    """Cloudflare Worker logs API.

    Worker trả JSON:
        - list trực tiếp [{to, subject, body, date, ...}, ...]
        - hoặc dict {messages|items|logs|emails|data: [...]}
    """

    def __init__(self, *, logs_url: str, api_key: str | None, insecure_tls: bool = False):
        if not logs_url:
            raise ValueError("Worker logs_url is required")
        self.logs_url = logs_url
        self.api_key = api_key
        self.insecure_tls = insecure_tls
        if insecure_tls:
            from config import warn_insecure_tls
            warn_insecure_tls("mail_providers.WorkerMailProvider")

    @staticmethod
    def _normalize(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("messages", "items", "logs", "emails", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        mailbox = recipient.strip().lower()
        if not mailbox:
            raise ValueError("recipient is required")

        headers: dict[str, str] = {"Accept": "application/json", "User-Agent": _BROWSER_UA}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.insecure_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            verify: Any = ctx
        else:
            verify = True

        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        log(f"[otp:worker] polling {mailbox} (timeout {timeout_seconds:.0f}s)")

        # Adaptive backoff: poll nhanh ngay khi vừa request OTP send (mail có
        # thể về trong 5-15s), giãn dần đến ``poll_interval_seconds`` ổn định.
        # Tiết kiệm 0-3s/signup so với fixed interval khi mail về sớm.
        # Table: 1s → 2s → 3s → poll_interval (lặp). Cap = poll_interval.
        _initial_backoff = (1.0, 2.0, 3.0)

        async with httpx.AsyncClient(verify=verify, timeout=20.0, follow_redirects=True) as client:
            attempt = 0
            consecutive_errors = 0  # fail-fast khi worker endpoint down liên tục
            _max_consecutive = 3
            while True:
                attempt += 1
                try:
                    response = await client.get(
                        f"{self.logs_url}?mail={quote(mailbox)}",
                        headers=headers,
                    )
                    if response.status_code != 200:
                        log(f"[otp:worker] HTTP {response.status_code} attempt {attempt}")
                        consecutive_errors += 1
                        if consecutive_errors >= _max_consecutive:
                            raise TimeoutError(
                                f"Worker logs API HTTP error {consecutive_errors} lần liên tiếp "
                                f"(last status={response.status_code}) — endpoint có thể đang down"
                            )
                    else:
                        consecutive_errors = 0
                        messages = self._normalize(response.json())
                        # Sort mới nhất trước (helper xử lý case thiếu date).
                        _sort_messages_newest_first(messages)
                        for msg in messages:
                            msg_to = str(msg.get("to") or "").strip().lower()
                            if msg_to and msg_to != mailbox:
                                continue
                            # KHÔNG lọc theo thời gian: recipient là +alias DUY NHẤT
                            # cho mỗi phiên nên mailbox chỉ chứa mã của phiên này. Mã
                            # đã sort mới→cũ; caller (browser_phase) dedup qua tried_codes
                            # nên luôn thử mã mới nhất chưa thử. Lọc theo `date` (HME relay
                            # lệch giờ + poll_started reset sau resend) dễ loại nhầm mã đúng.
                            subject = str(msg.get("subject") or "")
                            body = (
                                msg.get("bodyText") or msg.get("text") or msg.get("body")
                                or msg.get("htmlBody") or msg.get("content") or msg.get("html") or ""
                            )
                            code = _extract_otp(subject, str(body))
                            if code:
                                log(f"[otp:worker] found {code} (attempt {attempt})")
                                return code
                except (httpx.HTTPError, ValueError) as exc:
                    consecutive_errors += 1
                    log(
                        f"[otp:worker] error attempt {attempt} "
                        f"({consecutive_errors}/{_max_consecutive}): {type(exc).__name__}: {exc!r}"
                    )
                    if consecutive_errors >= _max_consecutive:
                        raise TimeoutError(
                            f"Worker logs API network error {consecutive_errors} lần liên tiếp: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"OTP timeout after {timeout_seconds}s for {mailbox}")
                # Adaptive sleep: attempt 1→1s, 2→2s, 3→3s, 4+ → poll_interval.
                sleep_s = (
                    _initial_backoff[attempt - 1]
                    if attempt <= len(_initial_backoff)
                    else poll_interval_seconds
                )
                await asyncio.sleep(min(sleep_s, remaining))

    async def poll_all_codes(
        self,
        *,
        recipient: str,
        started_at: datetime,
        log,
    ) -> list[str]:
        """Lấy TẤT CẢ OTP codes mới (sau started_at) trong 1 lần call API.

        Return list unique codes theo thứ tự API trả về (có thể mới nhất trước hoặc sau
        tuỳ worker). Không block/poll — chỉ fetch 1 lần.
        Dùng cho case: sau khi nhận 1 code, fetch lại để bắt thêm mail delay.
        """
        mailbox = recipient.strip().lower()
        if not mailbox:
            return []

        headers: dict[str, str] = {"Accept": "application/json", "User-Agent": _BROWSER_UA}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.insecure_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            verify: Any = ctx
        else:
            verify = True

        try:
            async with httpx.AsyncClient(verify=verify, timeout=20.0, follow_redirects=True) as client:
                response = await client.get(
                    f"{self.logs_url}?mail={quote(mailbox)}",
                    headers=headers,
                )
                if response.status_code != 200:
                    return []
                messages = self._normalize(response.json())
                # Sort mới→cũ để caller thử mã mới nhất trước (mirror poll_otp).
                _sort_messages_newest_first(messages)
                codes: list[str] = []
                seen: set[str] = set()
                for msg in messages:
                    msg_to = str(msg.get("to") or "").strip().lower()
                    if msg_to and msg_to != mailbox:
                        continue
                    # KHÔNG lọc theo thời gian — xem giải thích ở poll_otp (alias duy
                    # nhất mỗi phiên + dedup tried_codes + thử mới→cũ ở caller).
                    subject = str(msg.get("subject") or "")
                    body = (
                        msg.get("bodyText") or msg.get("text") or msg.get("body")
                        or msg.get("htmlBody") or msg.get("content") or msg.get("html") or ""
                    )
                    code = _extract_otp(subject, str(body))
                    if code and code not in seen:
                        seen.add(code)
                        codes.append(code)
                        # Chỉ lấy tối đa N mã MỚI NHẤT — đã sort mới→cũ nên cắt sớm.
                        # Tránh thử dồn quá nhiều mã cũ (đã bị OpenAI vô hiệu).
                        if len(codes) >= _WORKER_MAX_CODES:
                            break
                return codes
        except Exception:
            return []


# ─────────────────────────────────────────────────────────────────────
# Outlook provider (Microsoft Graph)
# ─────────────────────────────────────────────────────────────────────


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default offline_access"

# Folder names dùng tìm OTP — Inbox + Junk vì OpenAI mail thi thoảng vào spam.
_OTP_FOLDERS = ("Inbox", "Junk Email")

# Microsoft refresh / Graph: timeout tổng 12s, connect 6s — đủ để fail nhanh + retry.
# read=12s là per-byte-interval, KHÔNG phải tổng response time.
# Hard cap tổng dùng asyncio.wait_for trong _ensure_access.
_OUTLOOK_HTTP_TIMEOUT = httpx.Timeout(connect=6.0, read=12.0, write=12.0, pool=6.0)
_OUTLOOK_REFRESH_TOTAL_TIMEOUT = 15.0  # hard cap cho toàn bộ token refresh (s)

# Sau N lần network/HTTP transient liên tiếp → coi combo này transient-dead trong run hiện tại.
# Raise terminal error để job kết thúc nhanh thay vì chờ OTP timeout (180s).
_OUTLOOK_CONNECT_FAIL_THRESHOLD = 3

# Grace window cho filter `receivedDateTime < started_at`. Đề phòng caller set
# `started_at` lệch sau khi mail đã thực sự về (ví dụ browser_phase đặt
# poll_started SAU khi đợi OTP form load → mail có thể về sớm hơn vài giây).
# 30s đủ rộng để bắt mail OTP đến nhanh, vẫn loại được mail từ session cũ.
_OUTLOOK_GRAPH_DATE_GRACE = timedelta(seconds=30)

# Auth-fail strings → combo dead vĩnh viễn (revoke / disabled / format invalid)
_OUTLOOK_AUTH_FATAL_KEYS = (
    "invalid_grant",
    "AADSTS50173",  # FreshTokenNeeded — refresh token revoked
    "AADSTS70008",  # Refresh token expired
    "AADSTS50034",  # User account does not exist
    "AADSTS50057",  # User account is disabled
    "AADSTS700016",  # Application not found
    "unauthorized_client",
)


class OutlookComboError(Exception):
    """Combo Outlook parse/refresh fail (terminal — combo coi như dead)."""


class OutlookProviderUnavailable(Exception):
    """Outlook provider tạm thời không thể hoạt động (network/proxy fail).

    Khác với OutlookComboError ở chỗ: combo có thể vẫn sống, chỉ là network
    đến Microsoft đang fail. Caller có thể retry sau hoặc rotate proxy.
    """


class OutlookCombo:
    """Combo format: `email|password|refresh_token|client_id`.

    Component:
        email          — bpkknbrl2278@hotmail.com
        password       — không dùng cho refresh flow, lưu để re-login fallback
        refresh_token  — M.C535_BAY... (rotate sau mỗi refresh)
        client_id      — 8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2 (Outlook desktop pre-auth)
    """

    __slots__ = ("email", "password", "refresh_token", "client_id")

    def __init__(self, email: str, password: str, refresh_token: str, client_id: str):
        self.email = email
        self.password = password
        self.refresh_token = refresh_token
        self.client_id = client_id

    @classmethod
    def parse(cls, combo: str) -> "OutlookCombo":
        parts = combo.split("|")
        if len(parts) != 4:
            raise OutlookComboError(
                f"combo phải có 4 phần (email|password|refresh_token|client_id), nhận {len(parts)}"
            )
        email, password, refresh_token, client_id = (p.strip() for p in parts)
        if not email or "@" not in email:
            raise OutlookComboError(f"email không hợp lệ: {email!r}")
        if not refresh_token.startswith("M.C"):
            raise OutlookComboError("refresh_token không bắt đầu bằng 'M.C' (sai format)")
        if len(client_id) != 36 or client_id.count("-") != 4:
            raise OutlookComboError(f"client_id không phải UUID: {client_id!r}")
        return cls(email=email, password=password, refresh_token=refresh_token, client_id=client_id)


class OutlookMailProvider:
    """Microsoft Graph mail provider.

    - Tự động refresh token khi access expire.
    - Persist rotate refresh_token ra disk (`runtime/outlook_state/<email>.json`).
      Nếu không persist, lần sau dùng refresh_token cũ sẽ bị `invalid_grant`.
    """

    def __init__(
        self,
        *,
        combo: OutlookCombo,
        state_dir: Path,
        scope: str = _DEFAULT_SCOPE,
        proxy: str | None = None,
        combo_repo: ComboRepository | None = None,
    ):
        self.combo = combo
        self.scope = scope
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / f"{combo.email.replace('/', '_')}.json"
        self.proxy = proxy.strip() if isinstance(proxy, str) and proxy.strip() else None
        self._combo_repo = combo_repo
        self._access_token: str | None = None
        self._access_expires_at: float = 0.0
        # Hydrate state nếu đã từng refresh
        self._hydrate_state()

    def _hydrate_state(self) -> None:
        """Hydrate refresh_token từ persisted state.

        Khi combo_repo (SQLite) available: đọc từ DB (single source of truth).
        Khi không có combo_repo: fallback sang JSON state file (backward compat).
        """
        if self._combo_repo is not None:
            row = self._combo_repo.get_by_email(self.combo.email)
            if row is not None:
                latest = row.get("refresh_token")
                if isinstance(latest, str) and latest.startswith("M.C"):
                    self.combo.refresh_token = latest
            return
        # Fallback: JSON state file
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        latest = data.get("refresh_token")
        if isinstance(latest, str) and latest.startswith("M.C"):
            self.combo.refresh_token = latest

    def _persist_state(self, token_data: dict[str, Any]) -> None:
        # Prefer SQLite persist via ComboRepository — fail-fast khi DB là source of truth
        if self._combo_repo is not None:
            # Nếu combo_repo present → DB là authority. Fail = raise, không fallback JSON.
            self._combo_repo.update_refresh_token(
                self.combo.email, self.combo.refresh_token
            )
            return
        # Fallback: JSON file persist (backward compat khi không có combo_repo)
        record = {
            "email": self.combo.email,
            "client_id": self.combo.client_id,
            "refresh_token": self.combo.refresh_token,
            "last_refresh_at": datetime.now(timezone.utc).isoformat(),
            "expires_in": token_data.get("expires_in"),
            "scope": token_data.get("scope"),
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _safe_proxy(self) -> str | None:
        """Trả URL proxy đã ẩn user:pass cho log (không log credential)."""
        if not self.proxy:
            return None
        # Format: scheme://user:pass@host:port → scheme://***@host:port
        if "@" in self.proxy:
            scheme_split = self.proxy.split("://", 1)
            if len(scheme_split) == 2:
                scheme, rest = scheme_split
                _, _, host = rest.partition("@")
                return f"{scheme}://***@{host}"
        return self.proxy

    def _build_client(self) -> httpx.AsyncClient:
        """httpx client kèm proxy + timeout chuẩn cho Outlook."""
        kwargs: dict[str, Any] = {"timeout": _OUTLOOK_HTTP_TIMEOUT}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def _refresh_access(self, *, log) -> None:
        log(f"[otp:outlook] refreshing access token for {self.combo.email}"
            + (f" via proxy {self._safe_proxy()}" if self.proxy else ""))
        async with self._build_client() as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": self.combo.client_id,
                    "scope": self.scope,
                    "refresh_token": self.combo.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if response.status_code != 200:
            body = response.text[:500]
            # Phân biệt fatal (combo dead) vs transient (network blip / 5xx)
            fatal = any(key in body for key in _OUTLOOK_AUTH_FATAL_KEYS)
            if fatal or 400 <= response.status_code < 500:
                if "AADSTS70000" in body or "service abuse" in body.lower():
                    raise OutlookComboError(
                        f"Hotmail bị Microsoft khóa (service abuse mode) — combo dead, không retry. "
                        f"HTTP {response.status_code}"
                    )
                raise OutlookComboError(
                    f"refresh failed HTTP {response.status_code}: {body}"
                )
            raise OutlookProviderUnavailable(
                f"refresh transient HTTP {response.status_code}: {body[:200]}"
            )
        data = response.json()
        access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        if not access:
            raise OutlookComboError(f"refresh response missing access_token: {data}")
        # Persist trước, mutate in-memory sau — nếu persist fail, token cũ còn nguyên
        # tránh mất token khi process crash sau mutate nhưng trước persist.
        old_refresh = self.combo.refresh_token
        if new_refresh and new_refresh != old_refresh:
            self.combo.refresh_token = new_refresh
        try:
            self._persist_state(data)
        except Exception:
            # Rollback in-memory — DB vẫn giữ token cũ, đảm bảo nhất quán
            self.combo.refresh_token = old_refresh
            raise
        self._access_token = access
        self._access_expires_at = time.monotonic() + max(int(data.get("expires_in", 3600)) - 60, 60)

    async def _ensure_access(self, *, log) -> str:
        if self._access_token and time.monotonic() < self._access_expires_at:
            return self._access_token
        try:
            await asyncio.wait_for(
                self._refresh_access(log=log),
                timeout=_OUTLOOK_REFRESH_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise OutlookProviderUnavailable(
                f"refresh token request timed out after {_OUTLOOK_REFRESH_TOTAL_TIMEOUT}s "
                f"(login.microsoftonline.com không phản hồi)"
            )
        assert self._access_token
        return self._access_token

    async def _list_messages(
        self,
        *,
        client: httpx.AsyncClient,
        access_token: str,
        folder_name: str | None,
        top: int = 10,
    ) -> list[dict[str, Any]]:
        """Lấy `top` message mới nhất, optional theo tên folder."""
        if folder_name is None:
            url = f"{_GRAPH_BASE}/me/messages"
        else:
            # Filter folder by displayName
            folder_resp = await client.get(
                f"{_GRAPH_BASE}/me/mailFolders",
                params={"$filter": f"displayName eq '{folder_name}'"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            folder_resp.raise_for_status()
            folders = folder_resp.json().get("value", [])
            if not folders:
                return []
            folder_id = folders[0]["id"]
            url = f"{_GRAPH_BASE}/me/mailFolders/{folder_id}/messages"

        resp = await client.get(
            url,
            params={
                "$top": top,
                "$orderby": "receivedDateTime desc",
                "$select": "subject,from,receivedDateTime,bodyPreview,body",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        # Recipient phải khớp combo email — nếu không, OTP sẽ vào account khác.
        if recipient.strip().lower() != self.combo.email.strip().lower():
            log(
                f"[otp:outlook] WARNING recipient={recipient} != combo={self.combo.email} "
                f"— vẫn poll combo mailbox"
            )

        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        log(f"[otp:outlook] polling {self.combo.email} (timeout {timeout_seconds:.0f}s)"
            + (f" via proxy {self._safe_proxy()}" if self.proxy else " direct"))

        async with self._build_client() as client:
            attempt = 0
            consecutive_transient = 0
            while True:
                attempt += 1
                try:
                    access = await self._ensure_access(log=log)
                    # Strategy: query toàn bộ mailbox (folder=None) để bắt mail dù
                    # ở Inbox, Junk, hoặc folder lạ. Nhanh hơn và tin cậy hơn loop folder.
                    messages = await self._list_messages(
                        client=client, access_token=access, folder_name=None,
                        top=5,
                    )
                    consecutive_transient = 0  # reset khi 1 round thành công
                    threshold = (
                        started_at - _OUTLOOK_GRAPH_DATE_GRACE
                        if started_at is not None else None
                    )
                    for msg in messages:
                        received = _parse_dt(msg.get("receivedDateTime"))
                        # Chỉ accept mail received SAU started_at (trừ grace).
                        # Trước đây so trực tiếp `received < started_at` → khi
                        # caller set started_at vài giây sau khi mail đã về (vd
                        # browser_phase chờ form load 2-3s) thì mail đúng bị
                        # loại. Grace 30s cover khoảng lệch này, vẫn rớt mail
                        # OTP từ session cũ (cách đây vài phút trở lên).
                        if received is not None and threshold is not None:
                            if received < threshold:
                                continue
                        sender = (
                            (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
                        )
                        subject = msg.get("subject") or ""
                        body_obj = msg.get("body") or {}
                        body = body_obj.get("content") or msg.get("bodyPreview") or ""
                        code = _extract_otp(subject, body)
                        if code and (_is_openai_sender(sender) or "openai" in subject.lower()):
                            log(f"[otp:outlook] found {code} (sender={sender} attempt {attempt})")
                            return code
                        elif code:
                            log(
                                f"[otp:outlook] suspicious code {code} from {sender} "
                                f"subject={subject!r} — skip (non-OpenAI sender)"
                            )
                except (httpx.HTTPError, OutlookProviderUnavailable) as exc:
                    consecutive_transient += 1
                    # Dùng repr để bắt được cả ConnectTimeout("") không có message.
                    log(
                        f"[otp:outlook] network error attempt {attempt}"
                        f" ({consecutive_transient}/{_OUTLOOK_CONNECT_FAIL_THRESHOLD}): "
                        f"{type(exc).__name__}: {exc!r}"
                    )
                    if consecutive_transient >= _OUTLOOK_CONNECT_FAIL_THRESHOLD:
                        # Không thể kết nối Microsoft → bail nhanh thay vì chờ hết OTP timeout
                        raise OutlookProviderUnavailable(
                            f"connect Microsoft thất bại {consecutive_transient} lần liên tiếp "
                            f"(proxy={self._safe_proxy() or 'direct'}). Last error: "
                            f"{type(exc).__name__}: {exc!r}"
                        ) from exc
                except OutlookComboError as exc:
                    log(f"[otp:outlook] auth error attempt {attempt}: {exc}")
                    raise

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"OTP timeout after {timeout_seconds}s for {self.combo.email}"
                    )
                await asyncio.sleep(min(poll_interval_seconds, remaining))


# ─────────────────────────────────────────────────────────────────────
# DongVanFB Outlook provider (tools.dongvanfb.net API)
# ─────────────────────────────────────────────────────────────────────

_DONGVANFB_URL = "https://tools.dongvanfb.net/api/get_messages_oauth2"
_DONGVANFB_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=6.0)
_DONGVANFB_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://dongvanfb.net",
    "Referer": "https://dongvanfb.net/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}


# DongVanFB API trả `date` theo giờ VN (UTC+7), KHÔNG phải UTC. Phải gán đúng
# offset rồi convert sang UTC để so sánh với started_at (UTC). Trước đây gán nhầm
# tzinfo=UTC khiến lệch +7h → filter started_at vô hiệu, lấy nhầm OTP cũ.
_DONGVANFB_TZ = timezone(timedelta(hours=7))

# Grace window cho filter date của DongVanFB.
# - API trả date precision = phút (HH:MM) → mất tới 59s độ chính xác.
# - Mail OpenAI thi thoảng tới chỉ vài giây sau khi click submit → có thể về
#   trước khi caller kịp set `started_at`.
# Đặt 90s để cover cả 2 case mà vẫn loại được mail OTP cũ từ session reg trước
# (cách đây vài phút trở lên).
_DONGVANFB_DATE_GRACE = timedelta(seconds=90)


def _parse_dongvanfb_date(date_str: str) -> datetime | None:
    """Parse format 'HH:MM - DD/MM/YYYY' (giờ VN, UTC+7) → datetime UTC."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%H:%M - %d/%m/%Y")
        return dt.replace(tzinfo=_DONGVANFB_TZ).astimezone(timezone.utc)
    except ValueError:
        return None


class DongVanFBOutlookProvider:
    """Poll OTP Outlook qua API tools.dongvanfb.net/api/get_messages_oauth2.

    Request body: {"email": ..., "pass": ..., "refresh_token": ..., "client_id": ...}
    Response:
        {
            "email": "...",
            "status": true,
            "messages": [
                {
                    "from": "noreply@tm.openai.com",
                    "subject": "Your temporary ChatGPT login code",
                    "code": "",
                    "message": "<html>...957952...</html>",
                    "date": "19:20 - 20/05/2026"
                },
                ...
            ],
            "content": "Mail loaded successfully."
        }
    """

    def __init__(self, *, combo: OutlookCombo, proxy: str | None = None):
        self.combo = combo
        self.proxy = proxy.strip() if isinstance(proxy, str) and proxy.strip() else None

    def _build_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": _DONGVANFB_HTTP_TIMEOUT}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        log(f"[otp:dongvanfb] polling {self.combo.email} (timeout {timeout_seconds:.0f}s)")

        payload = {
            "email": self.combo.email,
            "pass": self.combo.password,
            "refresh_token": self.combo.refresh_token,
            "client_id": self.combo.client_id,
        }

        async with self._build_client() as client:
            attempt = 0
            consecutive_errors = 0
            # Filter mail theo date thay vì baseline-by-value.
            # Trước đây: vòng poll #1 capture mọi code OpenAI có sẵn = "cũ" rồi
            # các vòng sau chỉ accept code MỚI ngoài baseline. Bug: nếu mail
            # OTP về kịp trong vòng đầu (DongVanFB cache nhanh, mail server
            # cùng region) → code đúng bị nhầm là "cũ" → loop đến timeout.
            # Giờ: chỉ chấp nhận mail có date >= (started_at - grace). Mail OTP
            # cũ từ session trước (vài phút trở lên) sẽ rớt ngoài window.
            threshold = started_at - _DONGVANFB_DATE_GRACE
            log(
                f"[otp:dongvanfb] filter window: date >= {threshold.isoformat()} "
                f"(started_at={started_at.isoformat()}, grace={_DONGVANFB_DATE_GRACE.total_seconds():.0f}s)"
            )
            while True:
                attempt += 1
                try:
                    response = await client.post(
                        _DONGVANFB_URL,
                        headers=_DONGVANFB_HEADERS,
                        json=payload,
                    )
                    if response.status_code != 200:
                        log(f"[otp:dongvanfb] HTTP {response.status_code} attempt {attempt}")
                        consecutive_errors += 1
                    else:
                        data = response.json()

                        if not data.get("status"):
                            content = data.get("content", "")
                            consecutive_errors += 1
                            log(f"[otp:dongvanfb] status=false attempt {attempt}: {content}")
                        else:
                            consecutive_errors = 0
                            messages: list[dict] = data.get("messages") or []

                            # Sort mới nhất trước theo date (đã fix tz UTC+7).
                            def _msg_dt(m: dict) -> datetime:
                                return (
                                    _parse_dongvanfb_date(m.get("date") or "")
                                    or datetime.min.replace(tzinfo=timezone.utc)
                                )

                            messages_sorted = sorted(messages, key=_msg_dt, reverse=True)

                            stale_count = 0
                            for msg in messages_sorted:
                                sender = str(msg.get("from") or "")
                                subject = str(msg.get("subject") or "")
                                msg_dt = _parse_dongvanfb_date(msg.get("date") or "")
                                # Skip mail không parse được date — không thể
                                # phân biệt cũ/mới, chấp nhận false-positive an
                                # toàn hơn là nhặt nhầm code cũ.
                                if msg_dt is None:
                                    continue
                                if msg_dt < threshold:
                                    stale_count += 1
                                    continue
                                code = str(msg.get("code") or "").strip()
                                if not (code and len(code) == 6 and code.isdigit()):
                                    code = _extract_otp(subject, str(msg.get("message") or "")) or ""
                                if not code:
                                    continue
                                if not (_is_openai_sender(sender) or "openai" in subject.lower()):
                                    log(
                                        f"[otp:dongvanfb] suspicious code {code} "
                                        f"from {sender!r} — skip (non-OpenAI sender)"
                                    )
                                    continue
                                log(
                                    f"[otp:dongvanfb] found {code} "
                                    f"(date={msg_dt.isoformat()}, attempt {attempt})"
                                )
                                return code

                            if attempt <= 3 or attempt % 5 == 0:
                                log(
                                    f"[otp:dongvanfb] chưa có mail OpenAI mới "
                                    f"(total={len(messages)}, stale={stale_count}, "
                                    f"attempt {attempt})"
                                )

                    if consecutive_errors >= 3:
                        raise OutlookProviderUnavailable(
                            f"dongvanfb API thất bại {consecutive_errors} lần liên tiếp"
                        )

                except (httpx.HTTPError, ValueError) as exc:
                    consecutive_errors += 1
                    log(
                        f"[otp:dongvanfb] error attempt {attempt} "
                        f"({consecutive_errors}/3): {type(exc).__name__}: {exc!r}"
                    )
                    if consecutive_errors >= 3:
                        raise OutlookProviderUnavailable(
                            f"dongvanfb API lỗi network {consecutive_errors} lần liên tiếp: {exc}"
                        ) from exc

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"OTP timeout after {timeout_seconds}s for {self.combo.email}"
                    )
                await asyncio.sleep(min(poll_interval_seconds, remaining))


# ─────────────────────────────────────────────────────────────────────
# Gmail Advanced provider (checkgmail.live API)
# ─────────────────────────────────────────────────────────────────────


class GmailAdvancedParseError(Exception):
    """Parse input line fail cho Gmail Advanced mode."""


class GmailAdvancedProvider:
    """Provider poll OTP qua API checkgmail.live.

    Input format: email|api_url
    API response:
        {
            "ok": true,
            "order_id": "...",
            "service": "chatgpt",
            "email": "...",
            "status": "success",
            "mail_status": "live",
            "otp": "123456",       ← poll đến khi non-empty
            "otp_history": [...],
            "timeout_sec": 600,
            ...
        }

    Poll logic: gọi GET api_url liên tục, khi field `otp` có giá trị 6 số → return.
    Nếu `status` != "success" hoặc `ok` != true → báo lỗi.
    """

    def __init__(self, *, api_url: str, email: str = ""):
        if not api_url:
            raise ValueError("Gmail Advanced api_url is required")
        self.api_url = api_url
        self.email = email

    @classmethod
    def parse_line(cls, line: str) -> tuple[str, str]:
        """Parse line → (email, api_url).

        Hỗ trợ 2 format:
            - email|api_url  (cũ)
            - api_url        (chỉ paste link, email sẽ lấy từ API response)

        Raises GmailAdvancedParseError nếu format sai.
        """
        stripped = line.strip()
        # Format 1: chỉ URL (bắt đầu bằng http)
        if stripped.startswith(("http://", "https://")):
            return "", stripped
        # Format 2: email|url
        parts = stripped.split("|", 1)
        if len(parts) != 2:
            raise GmailAdvancedParseError(
                f"format phải là email|api_url hoặc chỉ api_url, nhận: {line[:80]}"
            )
        email_part = parts[0].strip()
        url_part = parts[1].strip()
        if not email_part or "@" not in email_part:
            raise GmailAdvancedParseError(f"email không hợp lệ: {email_part!r}")
        if not url_part.startswith(("http://", "https://")):
            raise GmailAdvancedParseError(f"api_url phải bắt đầu bằng http(s)://: {url_part[:60]}")
        return email_part, url_part

    async def pre_check(self, *, log) -> None:
        """Gọi API 1 lần để verify mail_status == 'live' trước khi chạy signup.

        Side-effects:
            - Nếu self.email rỗng (URL-only input) → tự fill email từ response.
            - Nếu mail_status != 'live' → raise ValueError (job fail ngay).
        """
        log(f"[otp:gmail_advanced] pre-check: {self.api_url}")
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            try:
                response = await client.get(self.api_url)
            except httpx.HTTPError as exc:
                raise ValueError(
                    f"Gmail Advanced pre-check failed (network): {type(exc).__name__}: {exc}"
                ) from exc

        if response.status_code != 200:
            raise ValueError(
                f"Gmail Advanced pre-check HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(f"Gmail Advanced pre-check: response không phải JSON") from exc

        # Extract email nếu chưa có (URL-only mode)
        api_email = str(data.get("email") or "").strip()
        if not self.email and api_email:
            self.email = api_email
            log(f"[otp:gmail_advanced] email from API: {self.email}")

        # Check ok field
        if not data.get("ok"):
            status = data.get("status", "unknown")
            raise ValueError(
                f"Gmail Advanced pre-check failed: ok=false, status={status}"
            )

        # Check mail_status
        mail_status = str(data.get("mail_status") or "").strip().lower()
        if mail_status != "live":
            raise ValueError(
                f"Gmail Advanced pre-check: mail_status='{mail_status}' (cần 'live') — "
                f"email={api_email or self.email}, dừng job."
            )

        log(f"[otp:gmail_advanced] pre-check OK: mail_status=live, email={self.email}")

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        log(f"[otp:gmail_advanced] polling {self.email} (timeout {timeout_seconds:.0f}s)")
        log(f"[otp:gmail_advanced] api: {self.api_url}")

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            attempt = 0
            consecutive_errors = 0  # fail-fast khi API down liên tục
            _max_consecutive = 3
            while True:
                attempt += 1
                try:
                    response = await client.get(self.api_url)
                    if response.status_code != 200:
                        log(f"[otp:gmail_advanced] HTTP {response.status_code} attempt {attempt}")
                        consecutive_errors += 1
                        if consecutive_errors >= _max_consecutive:
                            raise TimeoutError(
                                f"Gmail Advanced API HTTP error {consecutive_errors} lần liên tiếp "
                                f"(last status={response.status_code}) — endpoint có thể đang down"
                            )
                    else:
                        consecutive_errors = 0
                        data = response.json()
                        # Check API errors
                        if not data.get("ok"):
                            status = data.get("status", "unknown")
                            log(f"[otp:gmail_advanced] api ok=false status={status} attempt {attempt}")
                            # Nếu status rõ ràng là lỗi terminal → raise
                            if status in ("expired", "cancelled", "not_found"):
                                raise TimeoutError(
                                    f"Gmail Advanced API error: status={status} for {self.email}"
                                )
                        else:
                            otp = str(data.get("otp") or "").strip()
                            if otp and len(otp) == 6 and otp.isdigit():
                                log(f"[otp:gmail_advanced] found OTP {otp} (attempt {attempt})")
                                return otp

                            # Check otp_history — lấy code mới nhất nếu có
                            otp_history = data.get("otp_history")
                            if isinstance(otp_history, list) and otp_history:
                                # otp_history có thể là list string hoặc list dict
                                latest = otp_history[-1]
                                if isinstance(latest, dict):
                                    code = str(latest.get("otp") or latest.get("code") or "").strip()
                                else:
                                    code = str(latest).strip()
                                if code and len(code) == 6 and code.isdigit():
                                    log(f"[otp:gmail_advanced] found OTP from history {code} (attempt {attempt})")
                                    return code

                            if attempt <= 3 or attempt % 5 == 0:
                                log(f"[otp:gmail_advanced] waiting... otp='{otp}' attempt {attempt}")
                except (httpx.HTTPError, ValueError) as exc:
                    consecutive_errors += 1
                    log(
                        f"[otp:gmail_advanced] error attempt {attempt} "
                        f"({consecutive_errors}/{_max_consecutive}): {type(exc).__name__}: {exc!r}"
                    )
                    if consecutive_errors >= _max_consecutive:
                        raise TimeoutError(
                            f"Gmail Advanced API network error {consecutive_errors} lần liên tiếp: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"OTP timeout after {timeout_seconds}s for {self.email} (gmail_advanced)"
                    )
                await asyncio.sleep(min(poll_interval_seconds, remaining))


# ─────────────────────────────────────────────────────────────────────
# Outlook cascade provider (DongVanFB → fallback Microsoft Graph)
# ─────────────────────────────────────────────────────────────────────


# Cache email đã fail DongVanFB trong 1 lần process. Process tiếp theo cho cùng
# email sẽ bypass DongVanFB luôn → tiết kiệm consecutive_errors retry wait.
# Reset khi process restart (in-memory, không persist DB — DongVanFB có thể up lại).
_DONGVANFB_FAILED_EMAILS: set[str] = set()

# Ngưỡng tối thiểu để fallback Microsoft Graph còn ý nghĩa. < ngưỡng này thì
# re-raise luôn vì Microsoft refresh token round-trip ~3-6s → không đủ 1 vòng poll.
_CASCADE_FALLBACK_MIN_SECONDS: float = 30.0


def _mark_dongvanfb_failed(email: str) -> None:
    """Đánh dấu email vừa fail DongVanFB → process kế bypass DongVanFB."""
    _DONGVANFB_FAILED_EMAILS.add(email.strip().lower())


def _is_dongvanfb_recently_failed(email: str) -> bool:
    return email.strip().lower() in _DONGVANFB_FAILED_EMAILS


class OutlookCascadeProvider:
    """Wrapper cascade: thử DongVanFB trước, fallback Microsoft Graph nếu transient.

    Logic phân biệt:
        - OutlookProviderUnavailable (DongVanFB API down / network / 5xx)
                                                          → fallback Microsoft với remaining
        - TimeoutError (DongVanFB poll hết hạn nhưng API alive)
                                                          → fallback Microsoft với remaining
        - OutlookComboError                               → re-raise luôn (combo chết).
            DongVanFB không raise loại này; nếu xảy ra ở fallback Microsoft path
            (token revoked) thì cũng không có gì cứu được.

    Sticky: sau khi DongVanFB fail 1 lần cho email → process kế đi thẳng Microsoft.

    Tradeoff về token rotation: DongVanFB không tự rotate refresh_token. Khi
    DongVanFB là primary và alive liên tục, token Microsoft không được refresh
    → có thể stale dần. Khi nào DongVanFB fail → fallback Microsoft mới rotate
    qua `OutlookMailProvider._persist_state`. Đây là chấp nhận đánh đổi để
    DongVanFB nhận traffic chính (nhanh + ổn định hơn cho mục đích poll OTP).
    """

    def __init__(
        self,
        *,
        combo: OutlookCombo,
        state_dir: Path,
        proxy: str | None = None,
        combo_repo: "ComboRepository | None" = None,
    ):
        self.combo = combo
        self._microsoft = OutlookMailProvider(
            combo=combo, state_dir=state_dir, proxy=proxy, combo_repo=combo_repo,
        )
        self._dongvanfb = DongVanFBOutlookProvider(combo=combo, proxy=proxy)

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        email = self.combo.email
        deadline = time.monotonic() + max(timeout_seconds, 1.0)

        # Sticky bypass: email đã fail DongVanFB trong process này → đi thẳng Microsoft
        if _is_dongvanfb_recently_failed(email):
            log(f"[otp:cascade] {email} đã fail DongVanFB trước đó — dùng Microsoft Graph luôn")
            return await self._microsoft.poll_otp(
                recipient=recipient,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                log=log,
            )

        # Thử DongVanFB trước
        log(f"[otp:cascade] thử DongVanFB trước (timeout {timeout_seconds:.0f}s)")
        try:
            return await self._dongvanfb.poll_otp(
                recipient=recipient,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                log=log,
            )
        except OutlookProviderUnavailable as exc:
            # DongVanFB API down / network / 5xx liên tiếp 3 lần → fallback Microsoft.
            # KHÔNG nâng remaining lên min 30s — caller set deadline cứng,
            # kéo dài sẽ vi phạm contract. Nếu remaining < ngưỡng tối thiểu
            # cho 1 vòng Microsoft refresh+poll thì re-raise luôn.
            _mark_dongvanfb_failed(email)
            remaining = deadline - time.monotonic()
            if remaining < _CASCADE_FALLBACK_MIN_SECONDS:
                log(
                    f"[otp:cascade] DongVanFB unavailable ({exc}), "
                    f"remaining {remaining:.0f}s < {_CASCADE_FALLBACK_MIN_SECONDS:.0f}s "
                    f"không đủ cho Microsoft Graph — re-raise"
                )
                raise
            log(
                f"[otp:cascade] DongVanFB unavailable ({exc}) — "
                f"fallback Microsoft Graph với {remaining:.0f}s còn lại"
            )
        except TimeoutError as exc:
            # DongVanFB API alive nhưng OTP không về trong timeout. Có thể mail
            # delay phía Outlook server → cho Microsoft Graph 1 cơ hội với
            # remaining time nếu còn ≥ngưỡng.
            remaining = deadline - time.monotonic()
            if remaining < _CASCADE_FALLBACK_MIN_SECONDS:
                log(
                    f"[otp:cascade] DongVanFB timeout, remaining {remaining:.0f}s "
                    f"không đủ cho Microsoft Graph — re-raise"
                )
                raise
            _mark_dongvanfb_failed(email)
            log(
                f"[otp:cascade] DongVanFB poll timeout ({exc}) — "
                f"thử Microsoft Graph với {remaining:.0f}s còn lại"
            )

        # Fallback Microsoft Graph — dùng đúng remaining, không nâng floor.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"cascade deadline đã hết trước khi gọi Microsoft Graph ({remaining:.1f}s)"
            )
        return await self._microsoft.poll_otp(
            recipient=recipient,
            started_at=started_at,
            timeout_seconds=remaining,
            poll_interval_seconds=poll_interval_seconds,
            log=log,
        )


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def build_provider_worker(
    *, logs_url: str, api_key: str | None, insecure_tls: bool = False,
) -> WorkerMailProvider:
    return WorkerMailProvider(logs_url=logs_url, api_key=api_key, insecure_tls=insecure_tls)


def build_provider_outlook(
    *,
    combo: str,
    state_dir: Path,
    proxy: str | None = None,
    combo_repo: "ComboRepository | None" = None,
) -> OutlookCascadeProvider:
    """Build cascade provider: DongVanFB trước, fallback Microsoft Graph khi transient.

    Cascade logic ở `OutlookCascadeProvider.poll_otp` — caller không cần biết
    đang dùng provider nào. mail_provider="outlook" trong SignupRequest tự động
    được lợi từ fallback.
    """
    parsed = OutlookCombo.parse(combo)
    return OutlookCascadeProvider(
        combo=parsed, state_dir=state_dir, proxy=proxy, combo_repo=combo_repo,
    )


def build_provider_gmail_advanced(
    *, email: str, api_url: str,
) -> GmailAdvancedProvider:
    return GmailAdvancedProvider(api_url=api_url, email=email)


def build_provider_dongvanfb(
    *, combo: str, proxy: str | None = None,
) -> DongVanFBOutlookProvider:
    parsed = OutlookCombo.parse(combo)
    return DongVanFBOutlookProvider(combo=parsed, proxy=proxy)


# ─────────────────────────────────────────────────────────────────────
# China iCloud provider (icloudapi.xyz mailbox viewer)
# ─────────────────────────────────────────────────────────────────────
#
# Mỗi mailbox iCloud (alias HME) có URL viewer riêng do user mua qua dịch vụ
# tại icloudapi.xyz. URL pattern:
#       http(s)://icloudapi.xyz/show/<base64_token>/<email_url_encoded>
# Server trả `text/html`:
#   - Mailbox trống:  '<h1>错误</h1><p>No email found for recipient</p>'
#   - Có mail:        HTML page render mail mới nhất (subject + body raw)
# Vì URL gắn cứng vào 1 mailbox + token riêng, không cần lọc sender/date —
# bất kỳ 6 chữ số nào trên HTML đều là OTP của mailbox đó. Caller dedup qua
# `tried_codes` ở browser_phase / request_phase nên thử mã mới-nhất trước.

# Ngưỡng số code lấy về trong 1 lần `poll_all_codes` (mới→cũ theo thứ tự HTML).
_CHINA_ICLOUD_MAX_CODES = 5

# Marker server trả khi mailbox trống. Stripped HTML = "错误 No email found for recipient".
# Check substring là đủ: cả 2 marker xuất hiện cùng nhau, hiếm khi lẫn vào
# nội dung mail thật (mail OTP OpenAI viết tiếng Anh, không có "No email found").
_CHINA_ICLOUD_EMPTY_MARKERS: tuple[str, ...] = (
    "No email found for recipient",
    "No email found",
)


class ChinaICloudParseError(Exception):
    """Parse line fail cho China iCloud mode."""


class ChinaICloudProvider:
    """Poll OTP qua mailbox viewer URL của icloudapi.xyz.

    Format input mỗi dòng: `email----url` (separator 4 dấu gạch ngang).
    Provider GET URL → HTML → strip tag → regex 6 số (`_extract_otp`).
    KHÔNG lọc theo `started_at` (URL viewer = 1 mailbox riêng, mail mới luôn
    ghi đè trên page; caller dedup qua tried_codes).
    """

    SEPARATOR = "----"

    def __init__(self, *, email: str, api_url: str, proxy: str | None = None):
        if not email or "@" not in email:
            raise ValueError(f"china_icloud: invalid email {email!r}")
        if not api_url or not api_url.startswith(("http://", "https://")):
            raise ValueError(
                f"china_icloud: api_url phải http(s)://, nhận {api_url[:80]!r}"
            )
        self.email = email.strip().lower()
        self.api_url = api_url.strip()
        self.proxy = proxy

    @classmethod
    def parse_line(cls, line: str) -> tuple[str, str]:
        """Parse `email{SEPARATOR}url` → (email, api_url).

        Raise ChinaICloudParseError nếu format sai.
        """
        stripped = line.strip()
        if not stripped:
            raise ChinaICloudParseError("dòng trống")
        if cls.SEPARATOR not in stripped:
            raise ChinaICloudParseError(
                f"format phải là email{cls.SEPARATOR}url, nhận: {line[:80]}"
            )
        email_part, _, url_part = stripped.partition(cls.SEPARATOR)
        email_part = email_part.strip()
        url_part = url_part.strip()
        if "@" not in email_part or " " in email_part:
            raise ChinaICloudParseError(f"email không hợp lệ: {email_part!r}")
        if not url_part.startswith(("http://", "https://")):
            raise ChinaICloudParseError(
                f"url phải bắt đầu http(s)://: {url_part[:60]}"
            )
        return email_part, url_part

    @staticmethod
    def _is_empty_mailbox(text: str) -> bool:
        """True nếu HTML là dấu hiệu mailbox chưa có mail."""
        if not text:
            return True
        for marker in _CHINA_ICLOUD_EMPTY_MARKERS:
            if marker in text:
                return True
        return False

    @staticmethod
    def _client_kwargs() -> dict[str, Any]:
        return {
            "timeout": 20.0,
            "follow_redirects": True,
            "headers": {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": _BROWSER_UA,
            },
        }

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        log(
            f"[otp:china_icloud] polling {self.email} "
            f"(timeout {timeout_seconds:.0f}s)"
        )
        log(f"[otp:china_icloud] api: {self.api_url}")

        attempt = 0
        consecutive_errors = 0
        max_consecutive = 3

        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            while True:
                attempt += 1
                try:
                    resp = await client.get(self.api_url)
                    if resp.status_code != 200:
                        consecutive_errors += 1
                        log(
                            f"[otp:china_icloud] HTTP {resp.status_code} attempt {attempt}"
                        )
                        if consecutive_errors >= max_consecutive:
                            raise TimeoutError(
                                f"China iCloud HTTP error {consecutive_errors} lần liên tiếp "
                                f"(last status={resp.status_code})"
                            )
                    else:
                        consecutive_errors = 0
                        text = resp.text or ""
                        if self._is_empty_mailbox(text):
                            if attempt <= 3 or attempt % 5 == 0:
                                log(
                                    f"[otp:china_icloud] mailbox trống attempt {attempt}"
                                )
                        else:
                            code = _extract_otp("", text)
                            if code:
                                log(
                                    f"[otp:china_icloud] found {code} (attempt {attempt})"
                                )
                                return code
                            if attempt <= 3 or attempt % 5 == 0:
                                log(
                                    f"[otp:china_icloud] HTML có nội dung nhưng "
                                    f"chưa thấy code 6 số attempt {attempt}"
                                )
                except (httpx.HTTPError, ValueError) as exc:
                    consecutive_errors += 1
                    log(
                        f"[otp:china_icloud] error attempt {attempt} "
                        f"({consecutive_errors}/{max_consecutive}): "
                        f"{type(exc).__name__}: {exc!r}"
                    )
                    if consecutive_errors >= max_consecutive:
                        raise TimeoutError(
                            f"China iCloud network error {consecutive_errors} lần liên tiếp: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"OTP timeout after {timeout_seconds}s for {self.email} (china_icloud)"
                    )
                await asyncio.sleep(min(poll_interval_seconds, remaining))

    async def poll_all_codes(
        self,
        *,
        recipient: str,
        started_at: datetime,
        log,
    ) -> list[str]:
        """Lấy tất cả OTP codes phát hiện trên page (theo thứ tự xuất hiện).

        Mục đích: caller dùng để fallback thử mã khác khi mã hiện tại reject
        (mail-delay, multiple OTP requests). Không block; chỉ fetch 1 lần.
        """
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.get(self.api_url)
                if resp.status_code != 200:
                    return []
                text = resp.text or ""
                if self._is_empty_mailbox(text):
                    return []
                cleaned = re.sub(r"<[^>]*>", " ", text)
                cleaned = re.sub(r"https?://\S+", " ", cleaned)
                codes: list[str] = []
                seen: set[str] = set()
                for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", cleaned):
                    code = match.group(1)
                    if code in seen:
                        continue
                    seen.add(code)
                    codes.append(code)
                    if len(codes) >= _CHINA_ICLOUD_MAX_CODES:
                        break
                return codes
        except Exception:  # noqa: BLE001
            return []


def build_provider_china_icloud(
    *, email: str, api_url: str, proxy: str | None = None,
) -> ChinaICloudProvider:
    return ChinaICloudProvider(email=email, api_url=api_url, proxy=proxy)


# ─────────────────────────────────────────────────────────────────────
# iCloud v3 provider (icloud-cf-mail-v2 Worker — URL gắn cứng mailbox)
# ─────────────────────────────────────────────────────────────────────
#
# Khác WorkerMailProvider (v1) chỗ:
#   - v1: 1 URL chung `/logs?mail=<email>` + Bearer token.
#   - v3: mỗi mailbox có URL riêng `/readmail/<token>/data`, không cần auth.
# Response JSON giống v1 (cùng key `messages`/`logs`), schema field giống:
#     {
#       "email": "...",
#       "messages": [{"id","subject","date","htmlBody","receivedAt"}, ...],
#       "logs": [...],
#       "pagination": {...}
#     }
# Mỗi URL gắn cứng vào 1 alias HME → KHÔNG cần lọc theo `to` / `started_at`
# (alias chỉ chứa mail của phiên hiện tại; caller dedup qua tried_codes).

# Số mã OTP tối đa lấy về trong 1 lần `poll_all_codes`. Đồng nhất với Worker v1.
_ICLOUD_V3_MAX_CODES = 5

# Endpoint dấu hiệu hợp lệ — URL phải có pattern `/readmail/<token>/data` để
# tránh user paste nhầm URL của provider khác. Check lỏng (substring) chấp
# nhận cả http/https + bất kỳ host (proxy mirror, dev tunnel).
_ICLOUD_V3_URL_MARKER = "/readmail/"


class IcloudV3ParseError(Exception):
    """Parse line fail cho iCloud v3 mode."""


class IcloudV3Provider:
    """Poll OTP qua Worker v2 (icloud-cf-mail-v2) mailbox API.

    Format input mỗi dòng: ``email|api_url`` (separator ``|`` — 1 ký tự,
    giống Gmail Advanced).

    Provider GET ``api_url`` → JSON ``messages`` → extract OTP 6 số từ
    ``subject`` + ``htmlBody``. URL gắn cứng vào mailbox riêng nên KHÔNG
    cần lọc theo recipient/sender; caller dedup qua tried_codes.
    """

    SEPARATOR = "|"

    def __init__(self, *, email: str, api_url: str, proxy: str | None = None):
        if not email or "@" not in email:
            raise ValueError(f"icloud_v3: invalid email {email!r}")
        if not api_url or not api_url.startswith(("http://", "https://")):
            raise ValueError(
                f"icloud_v3: api_url phải http(s)://, nhận {api_url[:80]!r}"
            )
        if _ICLOUD_V3_URL_MARKER not in api_url:
            raise ValueError(
                f"icloud_v3: api_url thiếu marker {_ICLOUD_V3_URL_MARKER!r} "
                f"— nhận {api_url[:120]!r}"
            )
        self.email = email.strip().lower()
        self.api_url = api_url.strip()
        self.proxy = proxy.strip() if isinstance(proxy, str) and proxy.strip() else None

    @classmethod
    def parse_line(cls, line: str) -> tuple[str, str]:
        """Parse ``email|url`` → (email, api_url). Raise IcloudV3ParseError nếu sai."""
        stripped = (line or "").strip()
        if not stripped:
            raise IcloudV3ParseError("dòng trống")
        if cls.SEPARATOR not in stripped:
            raise IcloudV3ParseError(
                f"format phải là email{cls.SEPARATOR}url, nhận: {line[:80]}"
            )
        # Chỉ split 1 lần để tránh URL chứa ký tự `|` (an toàn theo RFC URL
        # vì `|` là reserved character, nhưng vẫn để chắc).
        email_part, _, url_part = stripped.partition(cls.SEPARATOR)
        email_part = email_part.strip()
        url_part = url_part.strip()
        if "@" not in email_part or " " in email_part:
            raise IcloudV3ParseError(f"email không hợp lệ: {email_part!r}")
        if not url_part.startswith(("http://", "https://")):
            raise IcloudV3ParseError(
                f"url phải bắt đầu http(s)://: {url_part[:60]}"
            )
        if _ICLOUD_V3_URL_MARKER not in url_part:
            raise IcloudV3ParseError(
                f"url thiếu marker {_ICLOUD_V3_URL_MARKER!r}, nhận: {url_part[:120]}"
            )
        return email_part, url_part

    @staticmethod
    def _normalize(payload: Any) -> list[dict[str, Any]]:
        """Lấy list `messages` từ response. Tái dụng key list của Worker v1."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("messages", "items", "logs", "emails", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    def _build_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": 20.0,
            "follow_redirects": True,
            "headers": {
                "Accept": "application/json,*/*",
                "User-Agent": _BROWSER_UA,
            },
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def poll_otp(
        self,
        *,
        recipient: str,
        started_at: datetime,
        timeout_seconds: float,
        poll_interval_seconds: float,
        log,
    ) -> str:
        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        log(
            f"[otp:icloud_v3] polling {self.email} "
            f"(timeout {timeout_seconds:.0f}s)"
        )
        log(f"[otp:icloud_v3] api: {self.api_url}")

        # Adaptive backoff giống Worker v1: 1s → 2s → 3s → poll_interval.
        _initial_backoff = (1.0, 2.0, 3.0)

        attempt = 0
        consecutive_errors = 0
        max_consecutive = 3

        async with self._build_client() as client:
            while True:
                attempt += 1
                try:
                    response = await client.get(self.api_url)
                    if response.status_code != 200:
                        consecutive_errors += 1
                        log(
                            f"[otp:icloud_v3] HTTP {response.status_code} "
                            f"attempt {attempt}"
                        )
                        if consecutive_errors >= max_consecutive:
                            raise TimeoutError(
                                f"iCloud v3 HTTP error {consecutive_errors} lần liên tiếp "
                                f"(last status={response.status_code})"
                            )
                    else:
                        consecutive_errors = 0
                        messages = self._normalize(response.json())
                        # Sort mới→cũ. Mail v3 luôn có `date`/`receivedAt`,
                        # helper sẽ chọn key có sẵn.
                        _sort_messages_newest_first(messages)
                        if not messages:
                            if attempt <= 3 or attempt % 5 == 0:
                                log(
                                    f"[otp:icloud_v3] mailbox trống attempt {attempt}"
                                )
                        else:
                            for msg in messages:
                                # KHÔNG lọc theo `to` (URL đã gắn cứng mailbox)
                                # và KHÔNG lọc theo thời gian (alias dùng 1 phiên).
                                subject = str(msg.get("subject") or "")
                                body = (
                                    msg.get("htmlBody")
                                    or msg.get("bodyText")
                                    or msg.get("text")
                                    or msg.get("body")
                                    or msg.get("content")
                                    or msg.get("html")
                                    or ""
                                )
                                code = _extract_otp(subject, str(body))
                                if code:
                                    log(
                                        f"[otp:icloud_v3] found {code} "
                                        f"(attempt {attempt})"
                                    )
                                    return code
                            if attempt <= 3 or attempt % 5 == 0:
                                log(
                                    f"[otp:icloud_v3] có {len(messages)} mail nhưng "
                                    f"chưa thấy code 6 số (attempt {attempt})"
                                )
                except (httpx.HTTPError, ValueError) as exc:
                    consecutive_errors += 1
                    log(
                        f"[otp:icloud_v3] error attempt {attempt} "
                        f"({consecutive_errors}/{max_consecutive}): "
                        f"{type(exc).__name__}: {exc!r}"
                    )
                    if consecutive_errors >= max_consecutive:
                        raise TimeoutError(
                            f"iCloud v3 network error {consecutive_errors} lần liên tiếp: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"OTP timeout after {timeout_seconds}s for {self.email} (icloud_v3)"
                    )
                sleep_s = (
                    _initial_backoff[attempt - 1]
                    if attempt <= len(_initial_backoff)
                    else poll_interval_seconds
                )
                await asyncio.sleep(min(sleep_s, remaining))

    async def poll_all_codes(
        self,
        *,
        recipient: str,
        started_at: datetime,
        log,
    ) -> list[str]:
        """Fetch 1 lần, lấy tất cả OTP codes (mới→cũ). Không block.

        Caller dùng để fallback thử mã khác khi mã hiện tại bị reject (mail
        delay, multiple OTP requests). Mirror ``WorkerMailProvider.poll_all_codes``.
        """
        try:
            async with self._build_client() as client:
                response = await client.get(self.api_url)
                if response.status_code != 200:
                    return []
                messages = self._normalize(response.json())
                _sort_messages_newest_first(messages)
                codes: list[str] = []
                seen: set[str] = set()
                for msg in messages:
                    subject = str(msg.get("subject") or "")
                    body = (
                        msg.get("htmlBody")
                        or msg.get("bodyText")
                        or msg.get("text")
                        or msg.get("body")
                        or msg.get("content")
                        or msg.get("html")
                        or ""
                    )
                    code = _extract_otp(subject, str(body))
                    if code and code not in seen:
                        seen.add(code)
                        codes.append(code)
                        if len(codes) >= _ICLOUD_V3_MAX_CODES:
                            break
                return codes
        except Exception:  # noqa: BLE001
            return []


def build_provider_icloud_v3(
    *, email: str, api_url: str, proxy: str | None = None,
) -> IcloudV3Provider:
    return IcloudV3Provider(email=email, api_url=api_url, proxy=proxy)
