"""Probe Stripe UPI checkout data for QR/intent payloads.

Usage:
    ACCOUNT_LINE="email|password|totp_secret" python3 test/probe_upi_qr.py

Optional:
    DB_PATH=runtime/data.db
    UPI_PROXY_FROM_STEP=3
    UPI_CHECKOUT_PROXY_URL=http://user:pass@vn-host:port
    UPI_PROMO=0
    UPI_QR_CONFIRM=1
    UPI_QR_APPROVE=1
    UPI_APPROVE_RETRIES=100
    UPI_APPROVE_DELAY=1
    UPI_APPROVE_PROXY_BATCH=3
    UPI_APPROVE_BACKEND_EXCEPTION_FAILS=2
    UPI_QR_VARIANTS=empty,flow_qr

This does not submit payment. It logs in, creates the India custom checkout,
calls Stripe init/elements, and searches returned JSON for UPI QR/intent data.
With UPI_QR_CONFIRM=1, it also tries a best-effort UPI confirm variant to
request Stripe's next_action QR payload. With UPI_QR_APPROVE=1, it calls
ChatGPT checkout approve after a successful confirm. Do not use these flags
casually.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_UPI_PROXY_FROM_STEP = 3
DEFAULT_APPROVE_PROXY_BATCH = 3
APPROVE_BACKEND_EXCEPTION_FAILS = 2
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RESET = "\033[0m"
_PROXY_DIRECT_VALUES = {"", "0", "false", "no", "none", "direct", "current"}
_ENV_NOT_SET = object()


class _PayloadMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.payload_message: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("id") == "payload":
            self.payload_message = values.get("data-message")


MATCH_TERMS = (
    "qr",
    "upi",
    "intent",
    "collect",
    "vpa",
    "next_action",
    "hosted_instructions",
    "image_url",
    "display_qr",
)
SENSITIVE_PATH_TERMS = (
    "access",
    "authorization",
    "client_secret",
    "cookie",
    "key",
    "password",
    "secret",
    "token",
)


def _mask_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    if len(local) <= 3:
        return f"{local[:1]}***@{domain}"
    return f"{local[:3]}***{local[-2:]}@{domain}"


def _mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    if "@" not in proxy:
        return proxy
    scheme, sep, rest = proxy.partition("://")
    host_part = rest.rsplit("@", 1)[-1]
    return f"{scheme}://***@{host_part}" if sep else "***@" + host_part


def _color_text(value: str, color: str) -> str:
    if os.environ.get("NO_COLOR"):
        return value
    return f"{color}{value}{ANSI_RESET}"


def _format_approve_result(attempt: dict[str, Any]) -> str:
    error_type = attempt.get("error_type")
    if error_type:
        error = str(attempt.get("error") or "")[:140]
        return f"result={_color_text('error', ANSI_RED)} error_type={error_type} error={error}"

    result = str(attempt.get("result") or "unknown")
    if result == "approved":
        result_text = _color_text(result, ANSI_GREEN)
    elif result == "blocked":
        result_text = _color_text(result, ANSI_RED)
    elif result == "exception":
        result_text = _color_text("backend_exception", ANSI_RED)
    else:
        result_text = _color_text(result, ANSI_YELLOW)
    return f"result={result_text} http_status={attempt.get('http_status')}"


def _pick_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attempt in reversed(attempts):
        if attempt.get("ok"):
            return attempt
    return attempts[-1] if attempts else None


def _format_amount(amount: int) -> str:
    return f"{amount} (₹{amount / 100:.2f})"


def _is_no_free_offer(promo: bool, amount: int) -> bool:
    return promo and amount > 0


def _is_backend_exception_result(attempt: dict[str, Any]) -> bool:
    return attempt.get("http_status") == 200 and attempt.get("result") == "exception"


def _summarize_confirm_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: attempt.get(key) for key in ("variant", "http_status", "ok", "keys", "error")}
        for attempt in attempts
    ]


def _summarize_approve_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: attempt.get(key)
            for key in (
                "variant",
                "attempt",
                "proxy",
                "http_status",
                "ok",
                "result",
                "error_type",
                "error",
                "keys",
            )
        }
        for attempt in attempts
    ]


def _summarize_refresh_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: attempt.get(key)
            for key in (
                "attempt",
                "proxy",
                "http_status",
                "ok",
                "error_type",
                "error",
                "keys",
            )
        }
        for attempt in attempts
    ]


def _make_output_paths() -> tuple[Path, Path]:
    out_dir = ROOT / "runtime" / "research_logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return out_dir / f"upi_qr_probe_{stamp}.json", out_dir / f"upi_qr_{stamp}.png"


def _print_no_free_offer_result(result: dict[str, Any], artifact_path: Path) -> None:
    print("[result] ok=False", flush=True)
    print(f"[result] account={result.get('email')}", flush=True)
    print(f"[result] error={_color_text('no free offer', ANSI_YELLOW)}", flush=True)
    print(f"[result] amount={_format_amount(int(result.get('amount') or 0))}", flush=True)
    print(f"[result] return_url={result.get('return_url')}", flush=True)
    print(f"[result] artifact={artifact_path}", flush=True)


def _print_approve_backend_exception_fail(result: dict[str, Any], artifact_path: Path) -> None:
    approve = _pick_attempt(result.get("approve_attempts") or [])
    print("[result] ok=False", flush=True)
    print(f"[result] account={result.get('email')}", flush=True)
    print("[result] error=approve backend_exception threshold", flush=True)
    print(f"[result] amount={_format_amount(int(result.get('amount') or 0))}", flush=True)
    print(f"[result] return_url={result.get('return_url')}", flush=True)
    print(f"[result] backend_exception_count={result.get('backend_exception_count')}", flush=True)
    print(f"[result] backend_exception_threshold={result.get('backend_exception_threshold')}", flush=True)
    if approve:
        print(
            "[result] last_approve="
            f"{approve.get('result') or approve.get('error_type')} "
            f"attempt={approve.get('attempt')} http={approve.get('http_status')} "
            f"proxy={approve.get('proxy')}",
            flush=True,
        )
    print(f"[result] artifact={artifact_path}", flush=True)


def _print_concise_result(result: dict[str, Any], artifact_path: Path) -> None:
    confirm = _pick_attempt(result.get("confirm_attempts") or [])
    approve = _pick_attempt(result.get("approve_attempts") or [])
    refresh = _pick_attempt(result.get("page_refresh_attempts") or [])
    qr = result.get("qr") if isinstance(result.get("qr"), dict) else {}

    print("[result] ok=True", flush=True)
    print(f"[result] account={result.get('email')}", flush=True)
    print(f"[result] amount={_format_amount(int(result.get('amount') or 0))}", flush=True)
    print(f"[result] return_url={result.get('return_url')}", flush=True)
    if confirm:
        print(
            "[result] confirm="
            f"{'ok' if confirm.get('ok') else 'fail'} "
            f"variant={confirm.get('variant')} http={confirm.get('http_status')}",
            flush=True,
        )
    if approve:
        status = approve.get("result") or approve.get("error_type") or "unknown"
        print(
            "[result] approve="
            f"{status} attempt={approve.get('attempt')} "
            f"http={approve.get('http_status')} proxy={approve.get('proxy')}",
            flush=True,
        )
    if refresh:
        refresh_status = "ok" if refresh.get("ok") else (refresh.get("error_type") or "fail")
        print(
            "[result] refresh="
            f"{refresh_status} http={refresh.get('http_status')} proxy={refresh.get('proxy')}",
            flush=True,
        )
    if qr.get("path"):
        print(f"[result] qr_path={qr.get('path')}", flush=True)
    else:
        print(f"[result] qr={qr.get('reason') or 'not_found'}", flush=True)
    if qr.get("source_url"):
        print(f"[result] qr_source_url={qr.get('source_url')}", flush=True)
    print(f"[result] artifact={artifact_path}", flush=True)
    print(f"[result] elapsed_seconds={result.get('elapsed_seconds')}", flush=True)


def _extract_hosted_instruction_upi_uri(html_text: str) -> str | None:
    parser = _PayloadMetaParser()
    parser.feed(html_text)
    message = parser.payload_message
    if not message:
        return None
    padded = message + ("=" * (-len(message) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    uri = payload.get("mobile_auth_url") if isinstance(payload, dict) else None
    return uri if isinstance(uri, str) and uri.startswith("upi:") else None


def _parse_account(line: str) -> tuple[str, str, str | None]:
    parts = [part.strip() for part in line.strip().split("|")]
    if len(parts) < 2:
        raise ValueError("ACCOUNT_LINE must be email|password|totp_secret")
    email = parts[0]
    password = parts[1]
    secret = parts[2] if len(parts) >= 3 and parts[2] else None
    if "@" not in email:
        raise ValueError("ACCOUNT_LINE email is invalid")
    if not password:
        raise ValueError("ACCOUNT_LINE password is empty")
    return email, password, secret


def _load_proxy_pool(db_path: str) -> list[str]:
    try:
        from gpt_signup_hybrid_new.db import get_engine
        from gpt_signup_hybrid_new.db.repositories import SettingsRepository
    except ModuleNotFoundError:
        from db import get_engine
        from db.repositories import SettingsRepository

    engine = get_engine(db_path)
    try:
        repo = SettingsRepository(engine)
        proxies = repo.get("proxy.pool") or []
    finally:
        engine.close()
    if not isinstance(proxies, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in proxies:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _first_proxy(proxies: list[str]) -> str | None:
    return proxies[0] if proxies else None


def _proxy_dict(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _proxy_for_step(proxy: str | None, from_step: int, step: int) -> dict[str, str] | None:
    if proxy and step >= from_step:
        return _proxy_dict(proxy)
    return None


def _proxy_url_for_retry(
    proxies: list[str],
    *,
    from_step: int,
    step: int,
    attempt: int,
    per_proxy_attempts: int,
) -> str | None:
    if step < from_step or not proxies:
        return None
    proxy_index = ((attempt - 1) // per_proxy_attempts) % len(proxies)
    return proxies[proxy_index]


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be int, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


def _read_proxy_env(*names: str) -> str | None | object:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        value = raw.strip()
        if value.lower() in _PROXY_DIRECT_VALUES:
            return None
        return value
    return _ENV_NOT_SET


def _resolve_checkout_proxy_url(proxy: str | None, from_step: int) -> str | None:
    override = _read_proxy_env("UPI_CHECKOUT_PROXY_URL", "UPI_CHECKOUT_PROXY")
    if override is not _ENV_NOT_SET:
        return override if isinstance(override, str) else None
    return proxy if proxy and 2 >= from_step else None


def _is_sensitive_path(path: str) -> bool:
    lower = path.lower()
    return any(term in lower for term in SENSITIVE_PATH_TERMS)


def _short_value(value: Any, path: str) -> Any:
    if _is_sensitive_path(path):
        return "[redacted]"
    if not isinstance(value, str):
        return value
    if len(value) <= 500:
        return value
    return f"{value[:260]}...{value[-120:]}"


def _find_matches(value: Any, *, source: str, path: str = "$") -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            key_lower = str(key).lower()
            if any(term in key_lower for term in MATCH_TERMS):
                matches.append({
                    "source": source,
                    "path": child_path,
                    "kind": "key",
                    "value": _short_value(item, child_path),
                })
            matches.extend(_find_matches(item, source=source, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(_find_matches(item, source=source, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        value_lower = value.lower()
        if any(term in value_lower for term in MATCH_TERMS):
            matches.append({
                "source": source,
                "path": path,
                "kind": "value",
                "value": _short_value(value, path),
            })
    return matches


def _find_upi_uri(matches: list[dict[str, Any]]) -> str | None:
    for match in matches:
        value = match.get("value")
        if isinstance(value, str) and value.lower().startswith("upi://"):
            return value
    return None


def _find_qr_image_url(matches: list[dict[str, Any]]) -> str | None:
    for match in matches:
        value = match.get("value")
        path = str(match.get("path") or "").lower()
        if (
            isinstance(value, str)
            and value.startswith("https://")
            and "qr" in path
            and (value.endswith(".png") or value.endswith(".svg") or "qr" in value.lower())
        ):
            return value
    return None


def _render_qr(payload: str, out_path: Path) -> dict[str, Any]:
    try:
        import qrcode
    except ModuleNotFoundError:
        return {
            "rendered": False,
            "reason": "python package qrcode is not installed",
        }
    image = qrcode.make(payload)
    image.save(out_path)
    return {
        "rendered": True,
        "path": str(out_path),
    }


def _redact_error(error: Any) -> Any:
    if not isinstance(error, dict):
        return str(error)[:500]
    allowed = {}
    for key in ("type", "code", "decline_code", "message", "param", "payment_intent"):
        if key in error:
            allowed[key] = _short_value(error.get(key), f"error.{key}")
    return allowed


def _upi_payload_for_variant(variant: str) -> dict[str, Any]:
    if variant == "flow_qr":
        return {"flow": "qr_code"}
    if variant == "qr_code":
        return {"qr_code": {}}
    if variant == "intent":
        return {"intent": "qr_code"}
    return {}


def _stripe_return_url(session_id: str) -> str:
    return f"https://checkout.stripe.com/c/pay/{session_id}"


def _extract_amount(init_data: dict[str, Any]) -> int:
    elements_options = init_data.get("elements_options")
    if isinstance(elements_options, dict) and isinstance(elements_options.get("amount"), int):
        return elements_options["amount"]
    total_summary = init_data.get("total_summary")
    if isinstance(total_summary, dict):
        for key in ("due", "total"):
            value = total_summary.get(key)
            if isinstance(value, int):
                return value
    invoice = init_data.get("invoice")
    if isinstance(invoice, dict):
        for key in ("amount_due", "total"):
            value = invoice.get(key)
            if isinstance(value, int):
                return value
    value = init_data.get("amount_total")
    return value if isinstance(value, int) else 0


async def _create_chatgpt_checkout_probe(
    sess: Any,
    *,
    access_token: str,
    promo: bool,
    log,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from gpt_signup_hybrid_new.pay_upi_http import (
        _CHATGPT_CHECKOUT_URL,
        _USER_AGENT,
        PayUpiError,
    )

    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "IN", "currency": "INR"},
        "checkout_ui_mode": "custom",
    }
    referer = "https://chatgpt.com/"
    if promo:
        referer = "https://chatgpt.com/?promo_campaign=plus-1-month-free"
        body["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "User-Agent": _USER_AGENT,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
        "OAI-Language": "en-IN",
    }
    log(f"  [2/6] POST /backend-api/payments/checkout promo={promo} proxy={'yes' if proxies else 'no'}")
    resp = await sess.post(
        _CHATGPT_CHECKOUT_URL,
        headers=headers,
        json=body,
        timeout=30,
        proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"checkout HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    needed = ("checkout_session_id", "publishable_key")
    miss = [key for key in needed if not data.get(key)]
    if miss:
        raise PayUpiError(f"checkout response missing {miss}: {data}")
    log(f"        ok cs={str(data['checkout_session_id'])[:18]}...")
    return data


async def _stripe_elements_session_probe(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    amount: int,
    log,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from gpt_signup_hybrid_new.pay_upi_http import (
        _STRIPE_ELEMENTS_URL,
        _STRIPE_VERSION,
        _USER_AGENT,
        PayUpiError,
    )

    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": "inr",
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "link",
        "deferred_intent[payment_method_types][2]": "upi",
        "currency": "inr",
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": "en",
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    log(f"  [4/6] GET /v1/elements/sessions amount={amount} proxy={'yes' if proxies else 'no'}")
    resp = await sess.get(
        _STRIPE_ELEMENTS_URL,
        headers=headers,
        params=params,
        timeout=30,
        proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"elements/sessions HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("session_id"):
        raise PayUpiError(f"elements/sessions missing session_id: keys={list(data)[:20]}")
    log(f"        ok elements_session={str(data['session_id'])[:22]}...")
    return data


async def _stripe_confirm_upi_qr_variant(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    init_data: dict[str, Any],
    elements_data: dict[str, Any],
    profile: dict[str, Any],
    email: str,
    amount: int,
    variant: str,
    log,
    token_config: Any | None,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from gpt_signup_hybrid_new.pay_upi_http import (
        _STRIPE_CONFIRM_URL,
        _STRIPE_VERSION,
        _USER_AGENT,
        _stripe_guid,
        _to_form,
    )

    elements_session_id = elements_data.get("session_id")
    elements_session_config_id = elements_data.get("config_id") or ""
    init_config_id = init_data.get("config_id") or ""
    ppage_id = init_data.get("id") or ""
    init_checksum = init_data["init_checksum"]

    if token_config is not None:
        from gpt_signup_hybrid_new import stripe_token as _st

        tokens = _st.build_token_fields(ppage_id=ppage_id, config=token_config)
        js_checksum = tokens["js_checksum"]
        rv_timestamp = tokens["rv_timestamp"]
    else:
        js_checksum = None
        rv_timestamp = None

    client_attribution_metadata = {
        "checkout_config_id": init_config_id,
        "checkout_session_id": session_id,
        "client_session_id": stripe_js_id,
        "elements_session_config_id": elements_session_config_id,
        "elements_session_id": elements_session_id,
        "merchant_integration_additional_elements": [
            "expressCheckout",
            "payment",
            "address",
        ],
        "merchant_integration_source": "checkout",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "custom",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    }
    pmd_client_attribution = dict(client_attribution_metadata)
    pmd_client_attribution["merchant_integration_source"] = "elements"
    pmd_client_attribution["merchant_integration_version"] = "2021"

    form = _to_form({
        "_stripe_version": _STRIPE_VERSION,
        "client_attribution_metadata": client_attribution_metadata,
        "elements_options_client": {
            "saved_payment_method": {
                "enable_redisplay": "auto",
                "enable_save": "auto",
            },
        },
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1",
                "custom_checkout_manual_approval_1",
            ],
            "elements_init_source": "custom_checkout",
            "is_aggregation_expected": "false",
            "locale": "en",
            "referrer_host": "chatgpt.com",
            "session_id": elements_session_id,
            "stripe_js_id": stripe_js_id,
        },
        "expected_amount": amount,
        "expected_payment_method_type": "upi",
        "guid": _stripe_guid(),
        "init_checksum": init_checksum,
        "js_checksum": js_checksum,
        "rv_timestamp": rv_timestamp,
        "passive_captcha_ekey": None,
        "passive_captcha_token": None,
        "key": publishable_key,
        "muid": _stripe_guid(),
        "sid": _stripe_guid(),
        "payment_method_data": {
            "billing_details": {
                "address": {
                    "city": profile["city"],
                    "country": "IN",
                    "line1": profile["address_line1"],
                    "postal_code": profile["postal_code"],
                    "state": profile["state"],
                },
                "email": email,
                "name": profile["name"],
            },
            "client_attribution_metadata": pmd_client_attribution,
            "payment_user_agent": (
                "stripe.js/e5ebd5e1e6; stripe-js-v3/e5ebd5e1e6; "
                "payment-element; deferred-intent"
            ),
            "referrer": "https://chatgpt.com",
            "time_on_page": int(time.time() * 1000) % 100000,
            "type": "upi",
            "upi": _upi_payload_for_variant(variant),
        },
        "return_url": _stripe_return_url(session_id),
        "version": "e5ebd5e1e6",
    })
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    log(f"  [5/6] POST /v1/payment_pages/{{cs}}/confirm variant={variant}")
    resp = await sess.post(
        _STRIPE_CONFIRM_URL.format(id=session_id),
        headers=headers,
        data=form,
        timeout=30,
        proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    return {
        "variant": variant,
        "http_status": resp.status_code,
        "ok": resp.status_code == 200,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "error": _redact_error(data.get("error")) if isinstance(data, dict) and data.get("error") else None,
        "data": data if resp.status_code == 200 else None,
    }


async def _stripe_payment_page_refresh_probe(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    init_data: dict[str, Any],
    elements_data: dict[str, Any],
    amount: int,
    log,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from gpt_signup_hybrid_new.pay_upi_http import (
        _STRIPE_PAGE_URL,
        _STRIPE_VERSION,
        _USER_AGENT,
        _to_form,
    )

    params = _to_form({
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1",
                "custom_checkout_manual_approval_1",
            ],
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": "en",
            "is_aggregation_expected": "false",
            "session_id": elements_data.get("session_id") or "",
        },
        "elements_options_client": {
            "saved_payment_method": {
                "enable_save": "auto",
                "enable_redisplay": "auto",
            },
        },
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
    })
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    log("  [5r/6] GET /v1/payment_pages/{cs} refresh")
    resp = await sess.get(
        _STRIPE_PAGE_URL.format(id=session_id),
        headers=headers,
        params=params,
        timeout=30,
        proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    return {
        "http_status": resp.status_code,
        "ok": resp.status_code == 200,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "error": _redact_error(data.get("error")) if isinstance(data, dict) and data.get("error") else None,
        "data": data if resp.status_code == 200 else None,
    }


async def _stripe_payment_page_refresh_retry_probe(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    init_data: dict[str, Any],
    elements_data: dict[str, Any],
    amount: int,
    log,
    proxy_pool: list[str],
    from_step: int,
) -> dict[str, Any]:
    candidates = proxy_pool if proxy_pool and 5 >= from_step else [None]
    last_attempt: dict[str, Any] | None = None
    for index, proxy_url in enumerate(candidates, start=1):
        log(f"        refresh attempt {index}/{len(candidates)} proxy={_mask_proxy(proxy_url)}")
        try:
            attempt = await _stripe_payment_page_refresh_probe(
                sess,
                session_id=session_id,
                publishable_key=publishable_key,
                stripe_js_id=stripe_js_id,
                init_data=init_data,
                elements_data=elements_data,
                amount=amount,
                log=log,
                proxies=_proxy_dict(proxy_url),
            )
        except Exception as exc:  # noqa: BLE001
            attempt = {
                "http_status": None,
                "ok": False,
                "keys": [],
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "data": None,
            }
        attempt["proxy"] = _mask_proxy(proxy_url)
        attempt["attempt"] = index
        last_attempt = attempt
        if attempt.get("ok"):
            return attempt
    return last_attempt or {
        "http_status": None,
        "ok": False,
        "keys": [],
        "error_type": "NoRefreshAttempt",
        "error": "no proxy candidates available",
        "data": None,
    }


async def _download_qr_image(
    sess: Any,
    *,
    url: str,
    out_path: Path,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    try:
        resp = await sess.get(url, timeout=30, proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return {
            "downloaded": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
    if resp.status_code != 200:
        return {
            "downloaded": False,
            "status": resp.status_code,
        }
    content_type = str(resp.headers.get("content-type") or "").lower()
    content = resp.content
    looks_like_html = "text/html" in content_type or content.lstrip().lower().startswith(b"<html")
    if looks_like_html:
        html_path = out_path.with_suffix(".html")
        html_path.write_bytes(content)
        html_text = content.decode("utf-8", errors="replace")
        upi_uri = _extract_hosted_instruction_upi_uri(html_text)
        if not upi_uri:
            return {
                "downloaded": False,
                "rendered": False,
                "reason": "hosted instructions HTML did not contain mobile_auth_url",
                "html_path": str(html_path),
            }
        result = _render_qr(upi_uri, out_path)
        result.update({
            "downloaded": False,
            "source": "hosted_instructions_html",
            "html_path": str(html_path),
        })
        if out_path.exists():
            result["bytes"] = out_path.stat().st_size
        return result

    out_path.write_bytes(content)
    return {
        "downloaded": True,
        "path": str(out_path),
        "bytes": len(content),
    }


async def _chatgpt_approve_checkout_probe(
    sess: Any,
    *,
    access_token: str,
    session_id: str,
    log,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from gpt_signup_hybrid_new.pay_upi_http import (
        _CHATGPT_APPROVE_URL,
        _USER_AGENT,
    )

    body = {
        "checkout_session_id": session_id,
        "processor_entity": "openai_llc",
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": f"https://chatgpt.com/checkout/openai_llc/{session_id}",
        "User-Agent": _USER_AGENT,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "OAI-Language": "en-IN",
    }
    log(f"  [6/6] POST /backend-api/payments/checkout/approve proxy={'yes' if proxies else 'no'}")
    resp = await sess.post(
        _CHATGPT_APPROVE_URL,
        headers=headers,
        json=body,
        timeout=30,
        proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    result = data.get("result") if isinstance(data, dict) else None
    return {
        "http_status": resp.status_code,
        "ok": resp.status_code == 200 and result == "approved",
        "result": result,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "data": data if resp.status_code == 200 else None,
    }


async def _run() -> int:
    from curl_cffi.requests import AsyncSession
    from gpt_signup_hybrid_new import stripe_token as _st
    from gpt_signup_hybrid_new.pay_upi_http import (
        _stripe_init,
    )
    from gpt_signup_hybrid_new.random_profile import random_india_profile
    from gpt_signup_hybrid_new.session_phase import get_session_pure_request

    started = monotonic()
    account_line = os.environ.get("ACCOUNT_LINE", "")
    db_path = os.environ.get("DB_PATH", "runtime/data.db")
    proxy_from_step = int(os.environ.get("UPI_PROXY_FROM_STEP", str(DEFAULT_UPI_PROXY_FROM_STEP)))
    promo = os.environ.get("UPI_PROMO", "1").strip().lower() not in {"0", "false", "no"}
    do_confirm = os.environ.get("UPI_QR_CONFIRM", "").strip().lower() in {"1", "true", "yes"}
    do_approve = os.environ.get("UPI_QR_APPROVE", "").strip().lower() in {"1", "true", "yes"}
    approve_retries = _read_positive_int_env("UPI_APPROVE_RETRIES", 1)
    approve_delay = max(0.0, float(os.environ.get("UPI_APPROVE_DELAY", "1")))
    approve_proxy_batch = _read_positive_int_env(
        "UPI_APPROVE_PROXY_BATCH",
        DEFAULT_APPROVE_PROXY_BATCH,
    )
    approve_backend_exception_fails = _read_positive_int_env(
        "UPI_APPROVE_BACKEND_EXCEPTION_FAILS",
        APPROVE_BACKEND_EXCEPTION_FAILS,
    )
    confirm_variants = [
        item.strip()
        for item in os.environ.get("UPI_QR_VARIANTS", "empty,flow_qr").split(",")
        if item.strip()
    ]
    email, password, secret = _parse_account(account_line)
    masked_email = _mask_email(email)
    proxy_pool = _load_proxy_pool(db_path)
    proxy = _first_proxy(proxy_pool)
    checkout_proxy = _resolve_checkout_proxy_url(proxy, proxy_from_step)
    masked_proxy = _mask_proxy(proxy)
    masked_proxy_pool = [_mask_proxy(item) for item in proxy_pool]
    masked_checkout_proxy = _mask_proxy(checkout_proxy)

    def log(message: str) -> None:
        safe = message.replace(email, masked_email)
        for raw_proxy, safe_proxy in zip(proxy_pool, masked_proxy_pool):
            safe = safe.replace(raw_proxy, safe_proxy)
        if checkout_proxy:
            safe = safe.replace(checkout_proxy, masked_checkout_proxy)
        print(safe, flush=True)

    print(f"[upi-qr] account={masked_email}", flush=True)
    print(f"[upi-qr] proxy={masked_proxy}", flush=True)
    print(f"[upi-qr] proxy_pool_count={len(proxy_pool)}", flush=True)
    print(f"[upi-qr] checkout_proxy={masked_checkout_proxy}", flush=True)
    print(f"[upi-qr] proxy_from_step={proxy_from_step}", flush=True)
    print(f"[upi-qr] promo={promo}", flush=True)
    print(f"[upi-qr] confirm={do_confirm}", flush=True)
    print(f"[upi-qr] approve={do_approve}", flush=True)
    if do_approve:
        print(f"[upi-qr] approve_retries={approve_retries} delay={approve_delay:g}s", flush=True)
        print(f"[upi-qr] approve_proxy_batch={approve_proxy_batch}", flush=True)
        print(
            f"[upi-qr] approve_backend_exception_fails={approve_backend_exception_fails}",
            flush=True,
        )

    session_data = await get_session_pure_request(
        email=email,
        password=password,
        secret=secret,
        proxy=proxy if proxy_from_step <= 1 else None,
        log=log,
    )
    access_token = session_data.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        print("[FAIL] session has no accessToken", flush=True)
        return 1

    stripe_js_id = str(uuid.uuid4())
    confirm_attempts: list[dict[str, Any]] = []
    approve_attempts: list[dict[str, Any]] = []
    page_refresh_attempts: list[dict[str, Any]] = []
    backend_exception_count = 0
    fatal_approve_error: str | None = None
    async with AsyncSession(impersonate="chrome136") as sess:
        checkout = await _create_chatgpt_checkout_probe(
            sess,
            access_token=access_token,
            promo=promo,
            log=log,
            proxies=_proxy_dict(checkout_proxy),
        )
        session_id = checkout["checkout_session_id"]
        return_url = _stripe_return_url(session_id)
        publishable_key = checkout["publishable_key"]
        init_data = await _stripe_init(
            sess,
            session_id=session_id,
            publishable_key=publishable_key,
            stripe_js_id=stripe_js_id,
            log=log,
            proxies=_proxy_for_step(proxy, proxy_from_step, 3),
        )
        amount = _extract_amount(init_data)
        print(f"[upi-qr] amount={amount}", flush=True)
        if _is_no_free_offer(promo, amount):
            artifact_path, _qr_path = _make_output_paths()
            result = {
                "ok": False,
                "email": masked_email,
                "proxy": masked_proxy,
                "proxy_pool_count": len(proxy_pool),
                "proxy_pool": masked_proxy_pool,
                "checkout_proxy": masked_checkout_proxy,
                "proxy_from_step": proxy_from_step,
                "promo": promo,
                "amount": amount,
                "error": "no free offer",
                "checkout_session": str(session_id)[:18] + "...",
                "return_url": return_url,
                "elapsed_seconds": round(monotonic() - started, 1),
            }
            artifact_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            _print_no_free_offer_result(result, artifact_path)
            return 1
        elements_data = await _stripe_elements_session_probe(
            sess,
            session_id=session_id,
            publishable_key=publishable_key,
            stripe_js_id=stripe_js_id,
            amount=amount,
            log=log,
            proxies=_proxy_for_step(proxy, proxy_from_step, 4),
        )
        if do_confirm:
            token_config = None
            try:
                token_config = await _st.extract_config_live(
                    sess,
                    log=log,
                    use_cache=True,
                    fallback_dir=ROOT / "runtime" / "cache" / "stripe_bundles_default",
                    proxies=None,
                )
                print("[upi-qr] token_config=ok", flush=True)
            except _st.StripeTokenExtractError as exc:
                print(f"[upi-qr] token_config=fail {str(exc)[:180]}", flush=True)

            profile = random_india_profile()
            for variant in confirm_variants:
                attempt = await _stripe_confirm_upi_qr_variant(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    init_data=init_data,
                    elements_data=elements_data,
                    profile=profile,
                    email=email,
                    amount=amount,
                    variant=variant,
                    log=log,
                    token_config=token_config,
                    proxies=_proxy_for_step(proxy, proxy_from_step, 5),
                )
                confirm_attempts.append(attempt)
                if attempt.get("ok"):
                    page_refresh_attempts.append(await _stripe_payment_page_refresh_retry_probe(
                        sess,
                        session_id=session_id,
                        publishable_key=publishable_key,
                        stripe_js_id=stripe_js_id,
                        init_data=init_data,
                        elements_data=elements_data,
                        amount=amount,
                        log=log,
                        proxy_pool=proxy_pool,
                        from_step=proxy_from_step,
                    ))
                    if do_approve:
                        approved = False
                        for approve_index in range(1, approve_retries + 1):
                            approve_proxy = _proxy_url_for_retry(
                                proxy_pool,
                                from_step=proxy_from_step,
                                step=6,
                                attempt=approve_index,
                                per_proxy_attempts=approve_proxy_batch,
                            )
                            log(
                                f"        approve attempt {approve_index}/{approve_retries} "
                                f"proxy={_mask_proxy(approve_proxy)}"
                            )
                            try:
                                approve_attempt = await _chatgpt_approve_checkout_probe(
                                    sess,
                                    access_token=access_token,
                                    session_id=session_id,
                                    log=log,
                                    proxies=_proxy_dict(approve_proxy),
                                )
                            except Exception as exc:  # noqa: BLE001
                                approve_attempt = {
                                    "http_status": None,
                                    "ok": False,
                                    "result": None,
                                    "keys": [],
                                    "error_type": type(exc).__name__,
                                    "error": str(exc)[:300],
                                    "data": None,
                                }
                            approve_attempt["variant"] = variant
                            approve_attempt["attempt"] = approve_index
                            approve_attempt["proxy"] = _mask_proxy(approve_proxy)
                            approve_attempts.append(approve_attempt)
                            log(f"        approve {_format_approve_result(approve_attempt)}")
                            if approve_attempt.get("ok"):
                                approved = True
                                break
                            if _is_backend_exception_result(approve_attempt):
                                backend_exception_count += 1
                                if backend_exception_count >= approve_backend_exception_fails:
                                    fatal_approve_error = "approve backend_exception threshold"
                                    log(
                                        "        approve fail=backend_exception_threshold "
                                        f"count={backend_exception_count}"
                                    )
                                    break
                            if approve_index < approve_retries:
                                await asyncio.sleep(approve_delay)
                        if not fatal_approve_error and (approved or approve_attempts):
                            page_refresh_attempts.append(await _stripe_payment_page_refresh_retry_probe(
                                sess,
                                session_id=session_id,
                                publishable_key=publishable_key,
                                stripe_js_id=stripe_js_id,
                                init_data=init_data,
                                elements_data=elements_data,
                                amount=amount,
                                log=log,
                                proxy_pool=proxy_pool,
                                from_step=proxy_from_step,
                            ))
                    break

    if fatal_approve_error:
        artifact_path, _qr_path = _make_output_paths()
        result = {
            "ok": False,
            "email": masked_email,
            "proxy": masked_proxy,
            "proxy_pool_count": len(proxy_pool),
            "proxy_pool": masked_proxy_pool,
            "checkout_proxy": masked_checkout_proxy,
            "proxy_from_step": proxy_from_step,
            "promo": promo,
            "amount": amount,
            "error": fatal_approve_error,
            "backend_exception_count": backend_exception_count,
            "backend_exception_threshold": approve_backend_exception_fails,
            "checkout_session": str(session_id)[:18] + "...",
            "return_url": return_url,
            "confirm_attempts": _summarize_confirm_attempts(confirm_attempts),
            "approve_attempts": _summarize_approve_attempts(approve_attempts),
            "page_refresh_attempts": _summarize_refresh_attempts(page_refresh_attempts),
            "elapsed_seconds": round(monotonic() - started, 1),
        }
        artifact_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        _print_approve_backend_exception_fail(result, artifact_path)
        return 1

    matches = []
    matches.extend(_find_matches(checkout, source="chatgpt_checkout"))
    matches.extend(_find_matches(init_data, source="stripe_init"))
    matches.extend(_find_matches(elements_data, source="stripe_elements"))
    for attempt in confirm_attempts:
        if attempt.get("data") is not None:
            matches.extend(_find_matches(attempt["data"], source=f"confirm:{attempt['variant']}"))
    for attempt in approve_attempts:
        if attempt.get("data") is not None:
            matches.extend(_find_matches(attempt["data"], source=f"approve:{attempt['variant']}"))
    for index, attempt in enumerate(page_refresh_attempts, start=1):
        if attempt.get("data") is not None:
            matches.extend(_find_matches(attempt["data"], source=f"payment_page_refresh:{index}"))
    upi_uri = _find_upi_uri(matches)
    qr_image_url = _find_qr_image_url(matches)

    artifact_path, qr_path = _make_output_paths()
    if qr_image_url:
        extension = ".svg" if qr_image_url.lower().endswith(".svg") else ".png"
        async with AsyncSession(impersonate="chrome136") as image_sess:
            qr_result = await _download_qr_image(
                image_sess,
                url=qr_image_url,
                out_path=qr_path.with_suffix(extension),
                proxies=_proxy_for_step(proxy, proxy_from_step, 5),
            )
        qr_result["source_url"] = qr_image_url
    elif upi_uri:
        qr_result = _render_qr(upi_uri, qr_path)
    else:
        qr_result = {"rendered": False, "reason": "no upi:// URI or QR image URL found"}

    result = {
        "ok": True,
        "email": masked_email,
        "proxy": masked_proxy,
        "proxy_pool_count": len(proxy_pool),
        "proxy_pool": masked_proxy_pool,
        "checkout_proxy": masked_checkout_proxy,
        "proxy_from_step": proxy_from_step,
        "promo": promo,
        "amount": amount,
        "checkout_session": str(session_id)[:18] + "...",
        "return_url": return_url,
        "match_count": len(matches),
        "has_upi_uri": bool(upi_uri),
        "has_qr_image_url": bool(qr_image_url),
        "promo": promo,
        "amount": amount,
        "qr": qr_result,
        "confirm_attempts": _summarize_confirm_attempts(confirm_attempts),
        "approve_attempts": _summarize_approve_attempts(approve_attempts),
        "page_refresh_attempts": _summarize_refresh_attempts(page_refresh_attempts),
        "matches": matches,
        "elapsed_seconds": round(monotonic() - started, 1),
    }
    artifact_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.environ.get("UPI_OUTPUT_JSON", "").strip().lower() in {"1", "true", "yes"}:
        print(json.dumps({
            "ok": True,
            "match_count": len(matches),
            "has_upi_uri": bool(upi_uri),
            "has_qr_image_url": bool(qr_image_url),
            "qr": qr_result,
            "return_url": result["return_url"],
            "confirm_attempts": result["confirm_attempts"],
            "approve_attempts": result["approve_attempts"],
            "page_refresh_attempts": result["page_refresh_attempts"],
            "artifact": str(artifact_path),
            "elapsed_seconds": result["elapsed_seconds"],
        }, ensure_ascii=False, indent=2), flush=True)
    else:
        _print_concise_result(result, artifact_path)
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("UPI_OUTPUT_JSON", "").strip().lower() in {"1", "true", "yes"}:
            print(json.dumps({
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }, ensure_ascii=False, indent=2), flush=True)
        else:
            print(f"[result] ok=False error_type={type(exc).__name__} error={str(exc)}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
