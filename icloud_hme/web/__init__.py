"""Web subsystem cho icloud-hme-pool.

Modules (runtime):
    log_buffer  — ``LogBuffer`` capped FIFO + asyncio pub-sub (R10.4–10.7).
    schemas     — Pydantic v2 schemas (RunRequest / RunStatus / LogEvent).

Modules (DEPRECATED — giữ cho backward-compat / test cũ):
    router      — Factory ``build_router(services)`` từ icloud-hme-pool R10
                  bản đầu. Runtime hiện mount ``web/icloud_routes.py``
                  (build_icloud_router) qua server.py — single source of
                  truth dùng middleware ``require_token`` của ``web/auth.py``.
                  File này CHỈ còn dùng cho ``test/check_router_run_auth.py``
                  test factory pattern; KHÔNG có endpoint nào của file này
                  được mount runtime.
    auth        — Bearer token verifier (env ``ICLOUD_API_AUTH_TOKEN``).
                  Đã thay bằng ``web/auth.py:require_token`` (env
                  ``GPT_SIGNUP_WEB_TOKEN``). Chỉ test cũ còn import.

Cảnh báo deprecation được emit khi consumer ngoài project import lazy
``build_router`` / ``verify_bearer_token`` qua subpackage này. Test cũ
import trực tiếp module → không trigger warning ở __init__ (intended).
"""

# Backward-compat re-export. KHÔNG xoá để giữ test cũ pass — emit warning
# DeprecationWarning lazily khi attribute được truy cập lần đầu để consumer
# tách module có thông báo.
import warnings as _warnings

from .router import build_router as _build_router  # noqa: F401
from .auth import verify_bearer_token as _verify_bearer_token  # noqa: F401


def __getattr__(name: str):
    """Lazy attribute hook để cảnh báo khi import từ subpackage path.

    Python gọi ``__getattr__`` khi ``from icloud_hme.web import build_router``
    không tìm thấy symbol trong module globals → emit DeprecationWarning
    rồi return symbol thực. Test cũ import trực tiếp ``.router`` /
    ``.auth`` không qua đây nên không bị spam warn.
    """
    if name == "build_router":
        _warnings.warn(
            "icloud_hme.web.build_router is DEPRECATED — runtime mount đã "
            "chuyển sang web/icloud_routes.py:build_icloud_router. "
            "File icloud_hme/web/router.py chỉ còn dùng cho test factory.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _build_router
    if name == "verify_bearer_token":
        _warnings.warn(
            "icloud_hme.web.verify_bearer_token is DEPRECATED — runtime auth "
            "đã chuyển sang web/auth.py:require_token (env "
            "GPT_SIGNUP_WEB_TOKEN). File icloud_hme/web/auth.py chỉ còn "
            "dùng cho test cũ.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _verify_bearer_token
    raise AttributeError(f"module 'icloud_hme.web' has no attribute {name!r}")


__all__ = ["build_router", "verify_bearer_token"]
