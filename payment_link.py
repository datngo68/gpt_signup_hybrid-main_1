"""Payment Link: lấy checkout URL pay.openai.com từ ChatGPT + Stripe API.

Flow:
    1. POST chatgpt.com/backend-api/payments/checkout (hosted mode)
       → CheckoutResponse (session_id, publishable_key, optional url)
    2. Nếu response có url chứa checkout.stripe.com/c/pay/ → replace host → return
    3. Nếu không → POST api.stripe.com/v1/payment_pages/{session_id}/init
       → stripe_hosted_url → replace host → return

UA + TLS persona: import từ ``user_agent_profile`` (Windows Chrome 145) — đồng
bộ với reg + UPI flow để cùng device persona xuyên suốt 1 account.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from curl_cffi.requests import AsyncSession

from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY as _UA_IMPERSONATE_PRIMARY,
    SEC_CH_UA as _SEC_CH_UA,
    SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
    SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    WINDOWS_USER_AGENT as _WINDOWS_USER_AGENT,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PaymentLinkError(Exception):
    """Base error for payment link operations."""
    pass


class SessionExpiredError(PaymentLinkError):
    """HTTP 401 from Checkout API — access token expired/revoked."""
    pass


class CloudflareBlockedError(PaymentLinkError):
    """HTTP 403 with Cloudflare challenge markers."""
    pass


class StripeInitError(PaymentLinkError):
    """Stripe init API failed or missing hosted_url."""
    pass


class GopayLinkError(PaymentLinkError):
    """Failed to obtain Midtrans GoPay redirect URL from Stripe checkout."""
    pass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class CheckoutResponse:
    """Parsed response from chatgpt.com/backend-api/payments/checkout."""

    checkout_session_id: str
    publishable_key: str
    client_secret: str | None = None
    url: str | None = None
    checkout_ui_mode: str | None = None


@dataclass
class GopayCheckoutContext:
    """Stripe init metadata required to confirm one GoPay checkout."""

    payment_url: str
    checkout_session_id: str
    publishable_key: str
    config_id: str
    init_checksum: str
    eid: str
    expected_amount: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_STRIPE_INIT_URL_TPL = "https://api.stripe.com/v1/payment_pages/{session_id}/init"
_CF_MARKERS = ("cf-chl", "just a moment", "cloudflare")
_IMPERSONATE = _UA_IMPERSONATE_PRIMARY
_CHECKOUT_MAX_ATTEMPTS = 3
_CHECKOUT_RETRY_DELAY_SECONDS = 0.5

# Region → billing_details mapping
REGION_BILLING: dict[str, dict[str, str]] = {
    "VN": {"country": "VN", "currency": "VND"},
    "ID": {"country": "ID", "currency": "IDR"},
    "IN": {"country": "IN", "currency": "INR"},
    "US": {"country": "US", "currency": "USD"},
}
DEFAULT_REGION = "VN"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replace_stripe_host(url: str) -> str:
    """Replace checkout.stripe.com → pay.openai.com, preserve path/query."""
    parsed = urlparse(url)
    if parsed.hostname == "checkout.stripe.com":
        replaced = parsed._replace(netloc="pay.openai.com")
        return urlunparse(replaced)
    return url


def _generate_stripe_js_id() -> str:
    """UUID v4 string for stripe_js_id parameter."""
    return str(uuid.uuid4())


def _check_response_error(status_code: int, body: str) -> None:
    """Raise appropriate error based on HTTP status code and body content.

    - 401 → SessionExpiredError
    - 403 + CF markers → CloudflareBlockedError
    - Other non-2xx → PaymentLinkError with status + first 300 chars body
    """
    if 200 <= status_code < 300:
        return

    if status_code == 401:
        raise SessionExpiredError(f"HTTP 401: session expired — {body[:300]}")

    if status_code == 403:
        body_lower = body.lower()
        if any(marker in body_lower for marker in _CF_MARKERS):
            raise CloudflareBlockedError(
                f"HTTP 403: Cloudflare block detected — {body[:300]}"
            )

    raise PaymentLinkError(f"HTTP {status_code}: {body[:300]}")


# ---------------------------------------------------------------------------
# Internal API calls
# ---------------------------------------------------------------------------


async def _call_chatgpt_checkout(
    session: AsyncSession,
    access_token: str,
    *,
    region: str = DEFAULT_REGION,
    promo_campaign: bool = True,
    timeout: float = 30.0,
) -> CheckoutResponse:
    """POST chatgpt.com/backend-api/payments/checkout with hosted mode payload."""
    billing = REGION_BILLING.get(region, REGION_BILLING[DEFAULT_REGION])

    referer = "https://chatgpt.com"
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": billing["country"],
            "currency": billing["currency"],
        },
        "checkout_ui_mode": "hosted",
    }
    if promo_campaign:
        referer += "/?promo_campaign=plus-1-month-free"
        payload["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }

    last_error: Exception | None = None
    for attempt in range(1, _CHECKOUT_MAX_ATTEMPTS + 1):
        try:
            resp = await session.post(
                _CHECKOUT_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except Exception as exc:
            last_error = exc
            if attempt < _CHECKOUT_MAX_ATTEMPTS:
                await asyncio.sleep(_CHECKOUT_RETRY_DELAY_SECONDS * attempt)
                continue
            raise PaymentLinkError(f"checkout request failed: {exc}") from exc

        body = resp.text
        if resp.status_code >= 500 and attempt < _CHECKOUT_MAX_ATTEMPTS:
            await asyncio.sleep(_CHECKOUT_RETRY_DELAY_SECONDS * attempt)
            continue
        break
    else:
        raise PaymentLinkError(f"checkout request failed: {last_error}")

    _check_response_error(resp.status_code, body)

    try:
        data = resp.json()
    except Exception as exc:
        raise PaymentLinkError(f"checkout JSON parse failed: {exc} — body: {body[:300]}") from exc

    session_id = data.get("checkout_session_id")
    pub_key = data.get("publishable_key")
    if not session_id or not pub_key:
        raise PaymentLinkError(
            f"checkout response missing required fields — "
            f"checkout_session_id={session_id!r}, publishable_key={pub_key!r}"
        )

    return CheckoutResponse(
        checkout_session_id=session_id,
        publishable_key=pub_key,
        client_secret=data.get("client_secret"),
        url=data.get("url"),
        checkout_ui_mode=data.get("checkout_ui_mode"),
    )


async def _call_stripe_init_data(
    session: AsyncSession,
    checkout_session_id: str,
    publishable_key: str,
    *,
    timeout: float = 30.0,
) -> dict:
    """POST api.stripe.com/v1/payment_pages/{session_id}/init → response data.

    Uses form-encoded data as Stripe expects.
    """
    url = _STRIPE_INIT_URL_TPL.format(session_id=checkout_session_id)
    stripe_js_id = _generate_stripe_js_id()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }
    # Form data y hệt Rust checkout.rs (urlencoded vẫn dùng được dict)
    form_data = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Saigon",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "en-US",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": publishable_key,
        "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1",
    }

    try:
        resp = await session.post(
            url,
            headers=headers,
            data=form_data,
            timeout=timeout,
        )
    except Exception as exc:
        raise PaymentLinkError(f"stripe init request failed: {exc}") from exc

    body = resp.text
    _check_response_error(resp.status_code, body)

    try:
        data = resp.json()
    except Exception as exc:
        raise StripeInitError(
            f"stripe init JSON parse failed: {exc} — body: {body[:300]}"
        ) from exc

    return data


async def _call_stripe_init(
    session: AsyncSession,
    checkout_session_id: str,
    publishable_key: str,
    *,
    timeout: float = 30.0,
) -> str:
    """POST Stripe init and return the hosted checkout URL."""
    data = await _call_stripe_init_data(
        session,
        checkout_session_id,
        publishable_key,
        timeout=timeout,
    )
    hosted_url = data.get("stripe_hosted_url")
    if not hosted_url:
        raise StripeInitError(
            f"stripe init response missing stripe_hosted_url — keys: {list(data.keys())}"
        )

    return hosted_url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _get_checkout_url(
    session: AsyncSession,
    access_token: str,
    *,
    timeout: float,
    region: str,
    promo_campaign: bool,
) -> tuple[str, str]:
    """Return the hosted checkout URL together with its live publishable key."""
    checkout = await _call_chatgpt_checkout(
        session,
        access_token,
        region=region,
        promo_campaign=promo_campaign,
        timeout=timeout,
    )

    if checkout.url:
        replaced = _replace_stripe_host(checkout.url)
        parsed = urlparse(replaced)
        if "/c/pay/" in (parsed.path or ""):
            return replaced, checkout.publishable_key

    hosted_url = await _call_stripe_init(
        session,
        checkout.checkout_session_id,
        checkout.publishable_key,
        timeout=timeout,
    )
    return _replace_stripe_host(hosted_url), checkout.publishable_key


def _validate_trial_init_data(init_data: dict) -> None:
    """Fail fast unless Stripe init proves this checkout is the IDR 0 trial."""
    payment_method_types = init_data.get("payment_method_types")
    if not isinstance(payment_method_types, list) or "gopay" not in payment_method_types:
        raise GopayLinkError("trial checkout does not expose the gopay payment method")

    elements_options = init_data.get("elements_options")
    amount = elements_options.get("amount") if isinstance(elements_options, dict) else None
    if amount != 0:
        raise GopayLinkError(f"trial checkout expected amount 0, got {amount!r}")

    invoice = init_data.get("invoice")
    invoice_total = invoice.get("total") if isinstance(invoice, dict) else None
    invoice_amount_due = invoice.get("amount_due") if isinstance(invoice, dict) else None
    if invoice_total != 0 or invoice_amount_due != 0:
        raise GopayLinkError(
            "trial invoice expected total=0 and amount_due=0, "
            f"got total={invoice_total!r} amount_due={invoice_amount_due!r}"
        )


async def _get_trial_checkout(
    session: AsyncSession,
    access_token: str,
    *,
    timeout: float,
) -> tuple[str, str]:
    """Return a validated one-month-free ID checkout URL and publishable key."""
    checkout = await _call_chatgpt_checkout(
        session,
        access_token,
        region="ID",
        promo_campaign=True,
        timeout=timeout,
    )
    init_data = await _call_stripe_init_data(
        session,
        checkout.checkout_session_id,
        checkout.publishable_key,
        timeout=timeout,
    )
    _validate_trial_init_data(init_data)

    hosted_url = init_data.get("stripe_hosted_url") or init_data.get("url")
    if not isinstance(hosted_url, str) or not hosted_url:
        raise StripeInitError(
            f"stripe init response missing stripe_hosted_url — keys: {list(init_data.keys())}"
        )
    return _replace_stripe_host(hosted_url), checkout.publishable_key


async def _get_trial_checkout_url(
    session: AsyncSession,
    access_token: str,
    *,
    timeout: float,
) -> str:
    """Return only a validated one-month-free ID checkout URL."""
    payment_url, _publishable_key = await _get_trial_checkout(
        session,
        access_token,
        timeout=timeout,
    )
    return payment_url


async def get_checkout_url(
    access_token: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    region: str = DEFAULT_REGION,
    promo_campaign: bool = True,
) -> str:
    """Main entry: access_token → pay.openai.com URL.

    Flow:
        1. POST checkout API → CheckoutResponse
        2. If response.url has checkout.stripe.com/c/pay/ → replace host → return
        3. Otherwise POST stripe init → get hosted_url → replace host → return

    Args:
        access_token: Bearer JWT from ChatGPT session.
        proxy: HTTP/HTTPS proxy URL (optional).
        timeout: Per-request timeout in seconds (default 30s).
        region: Region code (VN, ID, IN, US). Determines country + currency.
        promo_campaign: Apply the one-month-free campaign.

    Returns:
        Payment URL with pay.openai.com host.

    Raises:
        SessionExpiredError: HTTP 401
        CloudflareBlockedError: HTTP 403 + CF markers
        PaymentLinkError: other HTTP errors, timeout, parse errors
        StripeInitError: Stripe init failed or missing hosted_url
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        payment_url, _publishable_key = await _get_checkout_url(
            session,
            access_token,
            timeout=timeout,
            region=region,
            promo_campaign=promo_campaign,
        )
        return payment_url


# ---------------------------------------------------------------------------
# GoPay / Midtrans link extraction (Indonesia region)
# ---------------------------------------------------------------------------

_STRIPE_VERSION_GOPAY = "2020-08-27;custom_checkout_beta=v1"
_STRIPE_PM_URL = "https://api.stripe.com/v1/payment_methods"
_STRIPE_CONFIRM_URL_TPL = "https://api.stripe.com/v1/payment_pages/{session_id}/confirm"

# Billing info dùng cho tạo payment method GoPay.
_GOPAY_BILLING = {
    "name": "Mia Henderson",
    "email": "user@example.com",
    "country": "ID",
    "line1": "Jl Sudirman No 1",
    "city": "Jakarta",
    "postal_code": "10220",
    "state": "DKI Jakarta",
}


def _extract_cs_session_id(payment_url: str) -> str | None:
    """Extract cs_live_... or cs_test_... from pay.openai.com URL."""
    # URL format: https://pay.openai.com/c/pay/cs_live_xxxxx#...
    match = re.search(r'(cs_(?:live|test)_[A-Za-z0-9]+)', payment_url)
    return match.group(1) if match else None


def _extract_publishable_key(payment_url: str) -> str:
    """Return the legacy Stripe public-key fallback for URL-only callers."""
    return "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"


def _is_midtrans_url(value: str) -> bool:
    """Return True only for HTTPS URLs hosted by Midtrans."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = parsed.hostname
    return (
        parsed.scheme == "https"
        and host is not None
        and (host == "midtrans.com" or host.endswith(".midtrans.com"))
    )


def _build_gopay_checkout_context(
    payment_url: str,
    publishable_key: str,
    init_data: dict,
) -> GopayCheckoutContext:
    """Validate live Stripe init metadata before creating a GoPay method."""
    checkout_session_id = _extract_cs_session_id(payment_url)
    if not checkout_session_id:
        raise GopayLinkError(
            f"cannot extract checkout session ID from URL: {payment_url[:100]}"
        )

    payment_method_types = init_data.get("payment_method_types")
    if not isinstance(payment_method_types, list) or "gopay" not in payment_method_types:
        raise GopayLinkError("Stripe checkout does not expose the gopay payment method")

    elements_options = init_data.get("elements_options")
    amount = elements_options.get("amount") if isinstance(elements_options, dict) else None
    if not isinstance(amount, int):
        raise GopayLinkError("Stripe init response missing elements_options.amount")
    required = {}
    for key in ("config_id", "init_checksum", "eid"):
        value = init_data.get(key)
        if not isinstance(value, str) or not value:
            raise GopayLinkError(f"Stripe init response missing {key}")
        required[key] = value

    return GopayCheckoutContext(
        payment_url=payment_url,
        checkout_session_id=checkout_session_id,
        publishable_key=publishable_key,
        config_id=required["config_id"],
        init_checksum=required["init_checksum"],
        eid=required["eid"],
        expected_amount=str(amount),
    )


def _gopay_attribution_data(
    context: GopayCheckoutContext,
    *,
    client_session_id: str,
) -> dict[str, str]:
    return {
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": context.checkout_session_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[checkout_config_id]": context.config_id,
    }


def _build_gopay_payment_method_data(
    context: GopayCheckoutContext,
    *,
    guid: str,
    muid: str,
    sid: str,
    client_session_id: str,
) -> dict[str, str]:
    return {
        "type": "gopay",
        "billing_details[name]": _GOPAY_BILLING["name"],
        "billing_details[email]": _GOPAY_BILLING["email"],
        "billing_details[address][country]": _GOPAY_BILLING["country"],
        "billing_details[address][line1]": _GOPAY_BILLING["line1"],
        "billing_details[address][city]": _GOPAY_BILLING["city"],
        "billing_details[address][postal_code]": _GOPAY_BILLING["postal_code"],
        "billing_details[address][state]": _GOPAY_BILLING["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": _STRIPE_VERSION_GOPAY,
        "key": context.publishable_key,
        "payment_user_agent": "stripe.js/922d612e68; stripe-js-v3/922d612e68; checkout",
        **_gopay_attribution_data(context, client_session_id=client_session_id),
    }


def _build_gopay_confirm_data(
    context: GopayCheckoutContext,
    *,
    payment_method_id: str,
    guid: str,
    muid: str,
    sid: str,
    client_session_id: str,
) -> dict[str, str]:
    return_url = (
        f"https://pay.openai.com/c/pay/{context.checkout_session_id}"
        f"?redirect_pm_type=gopay&lid={uuid.uuid4()}&ui_mode=hosted"
    )
    return {
        "eid": context.eid,
        "payment_method": payment_method_id,
        "expected_amount": context.expected_amount,
        "consent[terms_of_service]": "accepted",
        "expected_payment_method_type": "gopay",
        "return_url": return_url,
        "_stripe_version": _STRIPE_VERSION_GOPAY,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": context.publishable_key,
        "version": "922d612e68",
        "init_checksum": context.init_checksum,
        **_gopay_attribution_data(context, client_session_id=client_session_id),
        "link_brand": "link",
    }


def _summarize_stripe_error(body: str) -> str:
    """Trích các field chẩn đoán quan trọng từ Stripe error response.

    Stripe nhét lý do thật vào nhiều chỗ lồng nhau (error.decline_code,
    error.advice_code, error.payment_method.*, last_setup_error...). Code cũ chỉ
    in 300 ký tự đầu nên thường cắt mất phần này. Hàm này parse JSON và gom các
    field hữu ích thành 1 dòng ngắn gọn; nếu không parse được thì trả raw body
    (cắt 600 ký tự thay vì 300 để giữ thêm ngữ cảnh).

    Returns:
        Chuỗi summary dạng "code=... decline_code=... message=...".
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return body[:600]

    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return body[:600]

    parts: list[str] = []
    for key in ("type", "code", "decline_code", "advice_code", "doc_url"):
        val = err.get(key)
        if val:
            parts.append(f"{key}={val}")

    msg = err.get("message")
    if msg:
        parts.append(f"message={msg!r}")

    # last_setup_error / last_payment_error thường chứa lý do GoPay/Midtrans thật.
    for nested_key in ("last_setup_error", "last_payment_error"):
        nested = err.get(nested_key)
        if isinstance(nested, dict):
            sub = {
                k: nested.get(k)
                for k in ("code", "decline_code", "message", "type")
                if nested.get(k)
            }
            if sub:
                parts.append(f"{nested_key}={sub}")

    return " ".join(parts) if parts else body[:600]


async def get_gopay_midtrans_url(
    payment_url: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    publishable_key: str | None = None,
) -> str:
    """Lấy Midtrans GoPay redirect URL từ Stripe checkout session URL.

    billing_details cố định (generic), KHÔNG nhận email/name từ caller.

    Flow:
        1. Extract cs_live_... từ payment_url
        2. POST /v1/payment_pages/{cs}/init → live confirm metadata
        3. POST /v1/payment_methods (type=gopay) → pm_...
        4. POST /v1/payment_pages/{cs}/confirm → redirect URL (pm-redirects.stripe.com)
        5. GET redirect URL (no follow) → 302 Location → Midtrans URL

    Args:
        payment_url: Stripe checkout URL (pay.openai.com/c/pay/cs_live_...)
        proxy: HTTP/HTTPS proxy URL (optional)
        timeout: Per-request timeout
        publishable_key: Live key returned with the checkout session. URL-only
            legacy callers omit this and use the public-key fallback.

    Returns:
        Midtrans snap URL: https://app.midtrans.com/snap/v4/redirection/{token}...

    Raises:
        GopayLinkError: any step fails
    """
    cs_session = _extract_cs_session_id(payment_url)
    if not cs_session:
        raise GopayLinkError(f"cannot extract checkout session ID from URL: {payment_url[:100]}")

    pk_live = publishable_key or _extract_publishable_key(payment_url)
    guid = uuid.uuid4().hex + "b41d9b"
    muid = uuid.uuid4().hex + "f10cae"
    sid = uuid.uuid4().hex + "6babdc"
    client_session_id = str(uuid.uuid4())

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
        "User-Agent": _WINDOWS_USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        # Step 1: Load the live Stripe Checkout contract.  The config id,
        # checksum, eid and amount are session-specific and must not be guessed.
        try:
            init_data = await _call_stripe_init_data(
                session,
                cs_session,
                pk_live,
                timeout=timeout,
            )
        except PaymentLinkError as exc:
            raise GopayLinkError(f"stripe init failed: {exc}") from exc
        context = _build_gopay_checkout_context(payment_url, pk_live, init_data)

        # Step 2: Create payment method (type=gopay)
        pm_data = _build_gopay_payment_method_data(
            context,
            guid=guid,
            muid=muid,
            sid=sid,
            client_session_id=client_session_id,
        )

        try:
            resp = await session.post(
                _STRIPE_PM_URL, headers=headers, data=pm_data, timeout=timeout,
            )
        except Exception as exc:
            raise GopayLinkError(f"create payment_method failed: {exc}") from exc

        if resp.status_code != 200:
            raise GopayLinkError(f"create payment_method HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            pm_result = resp.json()
        except Exception as exc:
            raise GopayLinkError(f"payment_method JSON parse failed: {exc}") from exc

        pm_id = pm_result.get("id")
        if not pm_id:
            raise GopayLinkError(f"payment_method response missing id: {list(pm_result.keys())}")

        # Step 3: Confirm payment using only live init metadata.
        confirm_data = _build_gopay_confirm_data(
            context,
            payment_method_id=pm_id,
            guid=guid,
            muid=muid,
            sid=sid,
            client_session_id=client_session_id,
        )

        confirm_url = _STRIPE_CONFIRM_URL_TPL.format(session_id=cs_session)
        try:
            resp = await session.post(
                confirm_url, headers=headers, data=confirm_data, timeout=timeout,
            )
        except Exception as exc:
            raise GopayLinkError(f"confirm request failed: {exc}") from exc

        if resp.status_code != 200:
            raise GopayLinkError(
                f"confirm HTTP {resp.status_code}: {_summarize_stripe_error(resp.text)}"
            )

        try:
            confirm_result = resp.json()
        except Exception as exc:
            raise GopayLinkError(f"confirm JSON parse failed: {exc}") from exc

        # Stripe có thể trả HTTP 200 nhưng body chứa error object (setup decline).
        # Phải fail-fast với lý do đầy đủ, không để rơi xuống nhánh "missing
        # pm-redirects URL" gây hiểu nhầm.
        if isinstance(confirm_result, dict) and confirm_result.get("error"):
            raise GopayLinkError(
                f"confirm declined: {_summarize_stripe_error(resp.text)}"
            )

        # Extract pm-redirects URL from response
        redirect_url: str | None = None
        confirm_str = resp.text
        match = re.search(r'https://pm-redirects\.stripe\.com/[^"\\]+', confirm_str)
        if match:
            redirect_url = match.group(0)

        if not redirect_url:
            raise GopayLinkError(
                f"confirm response missing pm-redirects URL — keys: {list(confirm_result.keys())[:10]}"
            )

        # Step 3: Follow redirect to get Midtrans URL
        try:
            resp = await session.get(
                redirect_url,
                headers={"User-Agent": headers["User-Agent"]},
                allow_redirects=False,
                timeout=timeout,
            )
        except Exception as exc:
            raise GopayLinkError(f"follow redirect failed: {exc}") from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            midtrans_url = resp.headers.get("Location") or resp.headers.get("location")
            if midtrans_url and _is_midtrans_url(midtrans_url):
                return midtrans_url
            raise GopayLinkError(
                f"redirect Location not Midtrans: {midtrans_url}"
            )

        # Fallback: parse body for midtrans URL
        body = resp.text[:3000]
        match = re.search(r'https://app\.midtrans\.com/snap/v[^\s"\'<>]+', body)
        if match:
            return match.group(0)

        raise GopayLinkError(
            f"cannot extract Midtrans URL from redirect response (status={resp.status_code})"
        )


async def get_gopay_url_from_access_token(
    access_token: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> tuple[str, str | None]:
    """Return a validated promo trial URL plus a separate GoPay URL.

    The returned ``payment_url`` is always a validated amount=0 trial checkout.
    Do not confirm GoPay against that checkout here: Stripe rejects zero-amount
    GoPay setup for this flow.  The Midtrans ``gopay_url`` is created from a
    separate non-promo ID checkout first, then a fresh trial checkout is created
    last so the returned trial URL remains active.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        paid_payment_url, paid_publishable_key = await _get_checkout_url(
            session,
            access_token,
            timeout=timeout,
            region="ID",
            promo_campaign=False,
        )

    gopay_url = await get_gopay_midtrans_url(
        paid_payment_url,
        proxy=proxy,
        timeout=timeout,
        publishable_key=paid_publishable_key,
    )

    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        payment_url, _trial_publishable_key = await _get_trial_checkout(
            session,
            access_token,
            timeout=timeout,
        )
    return payment_url, gopay_url
