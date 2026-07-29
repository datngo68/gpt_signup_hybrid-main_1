"""UPI session/cookie cache — file-based, scope hẹp CHỈ cho luồng UPI.

Tách riêng module này (không tái dụng `SessionProvider` cũ) để:
- Reg / Get Session / Get Link KHÔNG đụng vào cache → mỗi job login mới sạch.
- Phạm vi gọn, dễ audit: 1 file ~150 dòng, 4 method public + 1 helper revalidate.

Cache layout:
    runtime/upi_cookies/<instance_id>/<slug>-<hash>.json

- ``instance_id`` = stem của DB path (GSH_DB_PATH / engine.db_path) → nhiều
  instance song song không đụng cache nhau.
- ``<slug>-<hash>`` = ``safe_slug(email)-<sha256[:12]>`` chống collision khi
  email khác nhau slug map về cùng tên (vd '+' → '_').

Reuse flow (gọi từ ``_run_job`` UPI):
    1. ``revalidate_and_load(email, proxy)`` → trả session JSON đã mint token
       tươi nếu cookie còn live, else None.
    2. miss/expired/revalidate fail → caller chạy ``get_session_pure_request``
       như thường, rồi gọi ``save(...)`` NGAY sau khi login OK.

API public:
    cache = UpiSessionCache.singleton()
    cache.save(email, cookies, access_token, proxy)
    data = await cache.revalidate_and_load(email, proxy=...)
    cache.clear(email)
    cache.clear_all() -> int
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from session_phase import SessionError, fetch_session_via_http


_log = logging.getLogger(__name__)


# Tên cookie NextAuth session-token (split .0/.1 khi quá dài). Reuse-được khi
# trong list cookie có ít nhất 1 entry tên này với value khác rỗng.
_SESSION_TOKEN_COOKIE_NAMES: tuple[str, ...] = (
    "__Secure-next-auth.session-token",
    "__Secure-next-auth.session-token.0",
)

# TTL: cookie quá tuổi này → buộc login lại (kể cả revalidate OK cũng bỏ qua).
# 24h chuẩn theo policy ChatGPT NextAuth — cookie dài hơn rất dễ bị server
# soft-revoke giữa chừng. Hardcode để đơn giản; user có nút "Clear cookies"
# global nếu muốn ép login lại sớm.
_COOKIE_MAX_AGE_HOURS: int = 24


def _safe_email_slug(email: str) -> str:
    """Email → tên file an toàn (giữ chữ/số/._-@, ký tự khác → _)."""
    return "".join(c if (c.isalnum() or c in "._-@") else "_" for c in email) or "unknown"


def _has_session_token(cookies: Any) -> bool:
    if not isinstance(cookies, list):
        return False
    for c in cookies:
        if isinstance(c, dict) and c.get("name") in _SESSION_TOKEN_COOKIE_NAMES and c.get("value"):
            return True
    return False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve_instance_id() -> str:
    raw = os.environ.get("GSH_DB_PATH") or "data"
    return Path(raw).stem or "data"


def _default_runtime_dir() -> Path:
    from config import load_settings  # lazy: tránh circular
    return load_settings().runtime_dir


class UpiSessionCache:
    """File cache cookie/access_token cho UPI flow. Cô lập theo instance."""

    _singleton: "UpiSessionCache | None" = None

    @classmethod
    def singleton(cls) -> "UpiSessionCache":
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    def __init__(
        self,
        *,
        instance_id: str | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self._instance_id = instance_id or _resolve_instance_id()
        self._runtime_dir = Path(runtime_dir) if runtime_dir else _default_runtime_dir()
        self._cache_dir = self._runtime_dir / "upi_cookies" / self._instance_id

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def _path_for(self, email: str) -> Path:
        h = hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
        return self._cache_dir / f"{_safe_email_slug(email)}-{h}.json"

    # ── read ──────────────────────────────────────────────────────────
    def load(self, email: str) -> dict[str, Any] | None:
        """Đọc record. Fail-soft: thiếu/hỏng → None. Guard email-mismatch."""
        path = self._path_for(email)
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            rec = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(rec, dict) or rec.get("email") != email:
            return None
        return rec

    # ── write ─────────────────────────────────────────────────────────
    def save(
        self,
        email: str,
        *,
        cookies: list[dict[str, Any]],
        access_token: str | None,
        proxy: str | None,
    ) -> Path:
        """Atomic write (tmp + replace), chmod 0600. Latest-wins."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(email)
        payload = {
            "email": email,
            "cookies": cookies or [],
            "access_token": access_token,
            "proxy": proxy,
            "saved_at": _now_iso(),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # FS không hỗ trợ chmod (vd Windows) → best-effort
        tmp.replace(path)
        return path

    # ── clear ─────────────────────────────────────────────────────────
    def clear(self, email: str) -> bool:
        """Xoá 1 record. True nếu xoá được, False nếu không tồn tại / lỗi FS."""
        try:
            self._path_for(email).unlink()
            return True
        except (FileNotFoundError, OSError):
            return False

    def clear_all(self) -> int:
        """Xoá toàn bộ cache UPI của instance này. Trả số file đã xoá."""
        if not self._cache_dir.exists():
            return 0
        n = 0
        for p in self._cache_dir.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass  # best-effort
        # Clean tmp file rơi vãi (write fail giữa chừng).
        for p in self._cache_dir.glob("*.json.tmp"):
            try:
                p.unlink()
            except OSError:
                pass
        return n

    # ── reuse (revalidate + load) ─────────────────────────────────────
    async def revalidate_and_load(
        self,
        email: str,
        *,
        proxy: str | None = None,
    ) -> dict[str, Any] | None:
        """Thử reuse cookie cache:
            1. Load record. None nếu miss / quá TTL / không có session-token.
            2. Gọi /api/auth/session qua HTTP (mint token tươi).
            3. Trả session JSON shape: {"accessToken": ..., "__cookies": [...]}
               để caller dùng y như output của get_session_pure_request.
            4. Fail revalidate → trả None (caller fallback login thật).

        Args:
            email: account.
            proxy: proxy runtime (caller đang muốn dùng cho login). Tự thử
                proxy lưu trong cache trước, nếu fail mới retry với proxy này.
        """
        rec = self.load(email)
        if not rec:
            return None
        cookies = rec.get("cookies") or []
        if not _has_session_token(cookies):
            return None

        # TTL gate
        ts = rec.get("saved_at")
        if not ts:
            return None
        try:
            saved_dt = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            return None
        if datetime.now() - saved_dt > timedelta(hours=_COOKIE_MAX_AGE_HOURS):
            return None

        rec_proxy = rec.get("proxy")
        try:
            data = await fetch_session_via_http(cookies=cookies, proxy=rec_proxy or proxy)
        except SessionError:
            # Proxy lưu có thể chết → thử lại 1 lần với proxy runtime nếu khác.
            if rec_proxy and proxy and rec_proxy != proxy:
                try:
                    data = await fetch_session_via_http(cookies=cookies, proxy=proxy)
                except SessionError:
                    return None
            else:
                return None

        if not isinstance(data, dict) or not data.get("accessToken"):
            return None

        # Gắn __cookies (UPI cần để fill auth_sink/check_plan).
        data["__cookies"] = cookies

        # Cập nhật token tươi vào cache (best-effort — không block reuse nếu fail).
        try:
            self.save(
                email,
                cookies=cookies,
                access_token=data.get("accessToken"),
                proxy=rec_proxy,
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("upi-cache: update-after-revalidate fail (%s): %s", email, exc)
        return data
