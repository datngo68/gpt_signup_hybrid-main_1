"""Proxy line parsing + {SID} placeholder materialization.

Pool lưu **raw line/template**; mọi consumer feed cho curl_cffi/httpx/browser PHẢI
gọi ``materialize_proxy`` trước. Module pure-sync, no I/O, no network.

Format hỗ trợ:
  - ``host:port``                 → ``http://host:port`` (no-auth)
  - ``host:port:user:pass``       → ``http://user:pass@host:port``
  - ``host:port:user``            → ``http://user@host:port`` (pass rỗng)
  - ``scheme://user:pass@host:port`` → giữ nguyên (URL form, backward-compat)

Placeholder ``{SID}`` (hoặc ``{sid}`` — case-insensitive) ở user và/hoặc pass được
thay bằng **cùng 1 SID** ngẫu nhiên mỗi lần ``materialize_proxy`` (1 base line =
vô hạn sticky session → IP khác nhau).

Đây cũng là nơi đặt **canonical** ``mask_proxy`` (gộp 2 impl cũ ở manager + upi_runner)
để DRY và hết circular import.
"""
from __future__ import annotations

import random
import re
import string
from urllib.parse import quote

# Match cả {SID} lẫn {sid} (user import thường viết chữ thường: user-{sid}:pass).
_SID_RE = re.compile(r"\{sid\}", re.IGNORECASE)

# Strip credential khỏi proxy URL nhúng trong text bất kỳ (exception message,
# detail…) trước khi đưa ra log/SSE/UI. Phủ cả URL materialized (random SID) mà
# masker theo-string không biết trước để replace.
_PROXY_CRED_RE = re.compile(r"//[^/@\s]+@")


def sanitize_proxy_text(text: str) -> str:
    """Thay ``//user:pass@`` → ``//***@`` trong text bất kỳ (chống leak creds)."""
    return _PROXY_CRED_RE.sub("//***@", text)

_SID_ALPHABET = string.ascii_lowercase + string.digits


def gen_sid(length: int = 8) -> str:
    """Random sticky-session id ``[a-z0-9]{length}``."""
    return "".join(random.choice(_SID_ALPHABET) for _ in range(length))


def has_template(line: str) -> bool:
    """True nếu line chứa placeholder ``{SID}``/``{sid}`` (case-insensitive)."""
    return bool(line) and _SID_RE.search(line) is not None


def materialize_proxy(line: str, *, sid_len: int = 8) -> str:
    """Line/template → concrete proxy URL ``http://user:pass@host:port``.

    - Thay **mọi** ``{SID}``/``{sid}`` bằng **cùng 1 SID** (gen 1 lần/call, lazy).
    - Credential được URL-encode (``quote(safe="")``) → password chứa ``@``/``:``/``/``
      không phá ``urlparse`` ở browser path.
    - URL form (có ``://``) → passthrough (chỉ thay SID, không re-parse/re-encode).
    - Credential-at form ``user:pass@host:port`` (có ``@``, không ``://``) → prepend
      ``http://`` passthrough (format phổ biến BrightData/CliProxy/Oxylabs).

    Raise ``ValueError`` nếu line rỗng hoặc không đủ ``host:port``.
    """
    line = (line or "").strip()
    if not line:
        raise ValueError("empty proxy line")

    # SID thay TRƯỚC khi parse/quote → 1 SID phủ cả user+pass; SID là [a-z0-9]
    # không chứa ':' nên split phía dưới an toàn.
    if has_template(line):
        sid = gen_sid(sid_len)
        line = _SID_RE.sub(sid, line)

    # URL form: caller tự chuẩn → giữ nguyên (không re-encode để khỏi double-quote).
    if "://" in line:
        return line

    # ── Credential-at form: `[user[:pass]@]host:port` ──────────────────────
    # Nhiều proxy provider cung cấp format `user:pass@host:port` (KHÔNG có
    # scheme `://`). Nếu có `@` → tách bằng `@` cuối cùng; phần SAU phải
    # match `host:port` (port toàn digit) → prepend `http://` + passthrough.
    # Nếu phần SAU @ KHÔNG match host:port → fallback colon-split path cũ
    # (backward-compat cho edge case user@domain trong colon-form).
    if "@" in line:
        at_pos = line.rfind("@")
        after_at = line[at_pos + 1:]
        colon_pos = after_at.rfind(":")
        if colon_pos > 0:
            host = after_at[:colon_pos]
            port = after_at[colon_pos + 1:]
            if host and port and port.isdigit():
                # Confirmed: `user[:pass]@host:port` form → prepend http://
                return f"http://{line}"

    parts = line.split(":", 3)  # maxsplit=3 → pass giữ được dấu ':'
    n = len(parts)
    if n == 2:
        host, port = parts
        return f"http://{host}:{port}"
    if n == 3:
        host, port, user = parts
        return f"http://{quote(user, safe='')}@{host}:{port}"
    if n == 4:
        host, port, user, pwd = parts
        return f"http://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
    raise ValueError(f"invalid proxy format (need host:port[:user[:pass]]): {line!r}")


def mask_proxy(url: str | None) -> str:
    """Mask credential cho log/UI. None/rỗng → ``'direct'``.

    Xử cả 2 shape (pool lưu raw line colon-form, log dùng URL materialized):
      - URL form  ``scheme://user:pass@host:port`` → ``scheme://***@host:port``
      - colon raw ``host:port:user:pass``          → ``***@host:port``
      - no-auth (``host:port`` hoặc ``scheme://host:port``) → trả nguyên (không creds).
    """
    if not url:
        return "direct"
    scheme, sep, rest = url.partition("://")
    if sep:  # URL form (có scheme)
        if "@" not in rest:
            return url  # no-auth URL
        host_part = rest.rsplit("@", 1)[-1]
        return f"{scheme}://***@{host_part}"
    if "@" in url:  # "user:pass@host:port" (no scheme — hiếm)
        return f"***@{url.rsplit('@', 1)[-1]}"
    # colon-form raw line: host:port[:user[:pass]]
    parts = url.split(":", 3)
    if len(parts) >= 3:  # có credential → ẩn
        return f"***@{parts[0]}:{parts[1]}"
    return url  # host:port / host-only — không creds


__all__ = ["gen_sid", "has_template", "materialize_proxy", "mask_proxy", "sanitize_proxy_text"]
