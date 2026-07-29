"""Proxy health-check (probe) + ``acquire_live_proxy`` — the loop.

Gom SID-rotate / mark_dead / fallback-direct vào **1 chỗ** (DRY) để mọi flow login
(UPI Step1, Get Session, Get Link, Reg) dùng chung. Pool lưu raw line/template →
loop materialize SID tươi mỗi pick, probe nhẹ, xoay tới khi tìm được proxy live.

Concurrency: acquire bọc trong ``asyncio.Semaphore(N)`` (process-global, N =
``proxy.probe_concurrency``) — cho N job probe song song có giới hạn (user chạy
multi-job) thay vì serialize toàn bộ. Pool ``pick``/``mark_dead`` đã atomic riêng
qua ``ProxyPool._lock`` nên không cần serialize toàn chuỗi.

probe = L4 connectivity only — KHÔNG đại diện TLS path login thật (3/4 flow là
Camoufox/Firefox). Endpoint mặc định ``api64.ipify.org`` không fingerprint-gate.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .proxy_format import has_template, mask_proxy, materialize_proxy

# Default 6 knob (canonical) — .env (config.proxy_env_defaults) + Settings store
# (UI) override per-knob qua _load_proxy_knobs.
_DEFAULT_KNOBS: dict[str, object] = {
    "probe_endpoint": "https://api64.ipify.org",
    "probe_timeout": 6,
    "max_tries": 5,
    "sid_len": 8,
    "sid_retry_per_line": 2,
    "probe_concurrency": 4,
}

# Semaphore process-global: init lần đầu từ probe_concurrency; đổi knob cần restart.
_ACQUIRE_SEM: asyncio.Semaphore | None = None


def _acquire_sem(n: int) -> asyncio.Semaphore:
    global _ACQUIRE_SEM  # noqa: PLW0603
    if _ACQUIRE_SEM is None:
        _ACQUIRE_SEM = asyncio.Semaphore(max(1, int(n)))
    return _ACQUIRE_SEM


def _impersonate() -> str:
    try:
        from user_agent_profile import CURL_IMPERSONATE_PRIMARY
        return CURL_IMPERSONATE_PRIMARY
    except Exception:  # noqa: BLE001
        return "chrome145"


def _log(log, msg: str) -> None:
    if log:
        log(msg)


# ── Reason classifier (F-G) ──────────────────────────────────────────────

def _classify_exc(exc: BaseException) -> str:
    """Phân loại exception probe → ``"auth"`` (giết line) | ``"ip"`` (rotate SID).

    auth = host không resolve được / proxy auth fail → cả line hỏng.
    ip   = timeout/reset/refused/tunnel (conservative default) → IP-level, rotate SID.
    """
    msg = str(exc).lower()
    if any(m in msg for m in (
        "could not resolve host", "couldn't resolve host",
        "name or service not known", "curl: (6)",
    )):
        return "auth"
    if "proxy authentication" in msg or "407" in msg:
        return "auth"
    return "ip"


# ── Probe (F-G: 407 là HTTP status, KHÔNG exception) ──────────────────────

async def probe_proxy(
    url: str,
    *,
    endpoint: str,
    timeout: int | float,
    impersonate: str | None = None,
) -> tuple[bool, str]:
    """Probe nhẹ 1 proxy URL concrete → ``(ok, reason)``.

    reason ∈ {"ok", "auth", "ip"}. 407 về dưới dạng HTTP status (không raise) →
    bắt ở nhánh status, KHÔNG chờ exception marker.
    """
    from curl_cffi.requests import AsyncSession

    if impersonate is None:
        impersonate = _impersonate()
    try:
        async with AsyncSession(impersonate=impersonate) as session:
            r = await session.get(
                endpoint, proxies={"http": url, "https": url}, timeout=timeout
            )
        code = r.status_code
        if code // 100 == 2:
            return (True, "ok")
        if code == 407:
            return (False, "auth")
        return (False, "ip")  # status lạ khác → ip (rotate, không giết oan)
    except Exception as exc:  # noqa: BLE001
        return (False, _classify_exc(exc))


# ── The loop (F-I: Semaphore N; F-L: ValueError guard) ────────────────────

ProbeFn = Callable[..., Awaitable[tuple[bool, str]]]


async def acquire_live_proxy(
    pool,
    *,
    log=None,
    probe: ProbeFn | None = None,
    endpoint: str,
    timeout: int | float,
    max_tries: int,
    sid_len: int,
    sid_retry_per_line: int,
    probe_concurrency: int,
) -> tuple[str | None, str | None]:
    """Pick + probe + SID-rotate tới khi tìm được proxy live.

    Trả ``(concrete_url, raw_line)`` khi live; ``(None, None)`` khi pool rỗng/toàn
    dead **hoặc** cạn ``max_tries`` (caller → direct, fail-fast).

    - mark_dead **chỉ** ở reason ``"auth"`` (host/auth hỏng cả line).
    - reason ``"ip"`` + line có template → rotate SID (không giết line).
    - reason ``"ip"`` + non-template line → next line.
    - format rác (ValueError) → mark_dead line + next (không DoS job).
    """
    probe = probe or probe_proxy
    impersonate = _impersonate()
    async with _acquire_sem(probe_concurrency):
        if not pool.is_active():
            return (None, None)  # rỗng/toàn dead → direct
        tries = 0
        while tries < max_tries:
            line = pool.pick()  # atomic (pool._lock); cursor jitter chấp nhận
            if line is None:
                break
            sid_attempt = 0
            while sid_attempt <= sid_retry_per_line and tries < max_tries:
                tries += 1
                try:
                    url = materialize_proxy(line, sid_len=sid_len)
                except ValueError:
                    pool.mark_dead(line)
                    _log(log, f"[proxy] bad format {mask_proxy(line)} — drop line")
                    break
                ok, reason = await probe(
                    url, endpoint=endpoint, timeout=timeout, impersonate=impersonate
                )
                if ok:
                    _log(log, f"[proxy] live {mask_proxy(url)} (try {tries})")
                    return (url, line)
                if reason == "auth":
                    pool.mark_dead(line)
                    _log(log, f"[proxy] auth/host fail {mask_proxy(line)} — drop line")
                    break
                if not has_template(line):
                    _log(log, f"[proxy] {mask_proxy(url)} ip-level fail — next line")
                    break
                sid_attempt += 1
                _log(log, f"[proxy] {mask_proxy(url)} ip-level fail — rotate SID ({sid_attempt})")
        return (None, None)  # cạn max_tries → caller direct


# ── Knob loader (Settings store override > .env defaults > hardcoded) ──────

def _warn(log, key: str, raw, default) -> None:
    _log(log, f"[proxy] knob {key}={raw!r} invalid — dùng default {default!r}")


def _knob_int(d: dict, key: str, default: int, lo: int, hi: int, log=None) -> int:
    if key not in d or d[key] is None:
        return default
    raw = d[key]
    if isinstance(raw, bool):  # bool là subclass int — reject
        _warn(log, key, raw, default)
        return default
    try:
        val = int(raw)
    except (ValueError, TypeError):
        _warn(log, key, raw, default)
        return default
    if not (lo <= val <= hi):
        _warn(log, key, raw, default)
        return default
    return val


def _knob_str(d: dict, key: str, default: str, log=None) -> str:
    raw = d.get(key)
    if not isinstance(raw, str) or not raw.strip():
        if key in d and d[key] is not None:
            _warn(log, key, raw, default)
        return default
    raw = raw.strip()
    if not raw.lower().startswith("http"):
        _warn(log, key, raw, default)
        return default
    return raw


def _knob_bool(d: dict, key: str, default: bool, log=None) -> bool:
    if key not in d or d[key] is None:
        return default
    raw = d[key]
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    _warn(log, key, raw, default)
    return default


def _load_proxy_knobs(settings: dict | None, *, env_defaults: dict | None = None, log=None) -> dict:
    """6 knob từ Settings store (UI) > .env (env_defaults) > hardcoded default.

    Range validate ở đây (read-time): invalid → default + warning (KHÔNG clamp).
    ``env_defaults`` là dict store-key (``proxy.X``) raw từ ``config.proxy_env_defaults``;
    ưu tiên thấp hơn UI store, vẫn đi qua cùng validator.
    """
    merged = {**(env_defaults or {}), **(settings or {})}
    return {
        "probe_endpoint": _knob_str(merged, "proxy.probe_endpoint", _DEFAULT_KNOBS["probe_endpoint"], log),
        "probe_timeout": _knob_int(merged, "proxy.probe_timeout", _DEFAULT_KNOBS["probe_timeout"], 3, 15, log),
        "max_tries": _knob_int(merged, "proxy.max_tries", _DEFAULT_KNOBS["max_tries"], 1, 20, log),
        "sid_len": _knob_int(merged, "proxy.sid_len", _DEFAULT_KNOBS["sid_len"], 4, 32, log),
        "sid_retry_per_line": _knob_int(merged, "proxy.sid_retry_per_line", _DEFAULT_KNOBS["sid_retry_per_line"], 0, 10, log),
        "probe_concurrency": _knob_int(merged, "proxy.probe_concurrency", _DEFAULT_KNOBS["probe_concurrency"], 1, 10, log),
    }


def _acquire_kwargs(knobs: dict) -> dict:
    """Lọc knob → kwargs cho ``acquire_live_proxy``."""
    return {
        "endpoint": knobs["probe_endpoint"],
        "timeout": knobs["probe_timeout"],
        "max_tries": knobs["max_tries"],
        "sid_len": knobs["sid_len"],
        "sid_retry_per_line": knobs["sid_retry_per_line"],
        "probe_concurrency": knobs["probe_concurrency"],
    }


__all__ = [
    "probe_proxy",
    "acquire_live_proxy",
    "_load_proxy_knobs",
    "_acquire_kwargs",
    "_classify_exc",
]
