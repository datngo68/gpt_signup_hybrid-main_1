//! HTTP endpoints cho UPI flow — port 1:1 từ `pay_upi_http.py` +
//! `web/upi_runner.py` (variant logic riêng cho QR mode).

use crate::http::HttpClient;
use crate::random_profile::IndiaProfile;
use crate::stripe::forms::to_form;
use crate::stripe_token::{compute_js_checksum, compute_rv_timestamp, StripeTokenConfig};
use crate::upi::types::UpiAuth;
use crate::user_agent::{
    SEC_CH_UA, SEC_CH_UA_MOBILE, SEC_CH_UA_PLATFORM, WINDOWS_USER_AGENT,
};
use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::time::SystemTime;

const STRIPE_VERSION: &str = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1";
const CHATGPT_CHECKOUT_URL: &str = "https://chatgpt.com/backend-api/payments/checkout";
const CHATGPT_APPROVE_URL: &str = "https://chatgpt.com/backend-api/payments/checkout/approve";
const STRIPE_INIT_URL_TPL: &str = "https://api.stripe.com/v1/payment_pages/{id}/init";
const STRIPE_PAGE_URL_TPL: &str = "https://api.stripe.com/v1/payment_pages/{id}";
const STRIPE_CONFIRM_URL_TPL: &str = "https://api.stripe.com/v1/payment_pages/{id}/confirm";
const STRIPE_ELEMENTS_URL: &str = "https://api.stripe.com/v1/elements/sessions";

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

fn stripe_guid() -> String {
    // Format: <uuid>-<10 hex>. Python dùng `uuid.uuid4()` rồi `uuid.uuid4().hex[:10]`.
    let u1 = uuid::Uuid::new_v4().to_string();
    let u2 = uuid::Uuid::new_v4().simple().to_string();
    format!("{}{}", u1, &u2[..10])
}

fn stripe_return_url(session_id: &str) -> String {
    format!("https://checkout.stripe.com/c/pay/{}", session_id)
}

/// Cắt chuỗi an toàn theo char boundary (KHÔNG slice theo byte — body lỗi từ
/// Cloudflare/Stripe có thể chứa ký tự đa byte UTF-8, slice byte giữa ký tự sẽ
/// PANIC → task worker chết → leak slot/registry → user kẹt "không phản hồi").
fn safe_truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_string()
}

/// Warm Cloudflare cookie cho proxy mới — port từ Python `_warm_cf_cookie`
/// (FIX-CF-WARMUP-2026-06-20). GET `https://chatgpt.com/` qua proxy đó để CF
/// set `__cf_bm` trong jar → request approve kế tiếp KHÔNG bị 403 hit đầu.
///
/// Best-effort: lỗi gì cũng nuốt + log warn, KHÔNG fail approve loop. Caller
/// gọi 1 lần mỗi khi sang batch proxy mới (trước approve attempt đầu của batch).
pub async fn warm_cf_cookie(
    client: &HttpClient,
    proxy: Option<&str>,
    log: &crate::upi::runner::LogFn,
) {
    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert(
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    );
    headers.insert("Accept-Language", "en-IN,en;q=0.9");
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("sec-fetch-dest", "document");
    headers.insert("sec-fetch-mode", "navigate");
    headers.insert("sec-fetch-site", "none");
    headers.insert("sec-fetch-user", "?1");
    headers.insert("upgrade-insecure-requests", "1");

    let proxy_mask = proxy
        .map(crate::proxy_format::mask_proxy)
        .unwrap_or_else(|| "direct".into());
    match client.get_text("https://chatgpt.com/", &headers, proxy).await {
        Ok(r) => {
            // 200/403 đều có Set-Cookie __cf_bm; chỉ flag warn cho status lạ.
            let icon = if r.status == 200 || r.status == 403 {
                "OK  "
            } else {
                "WARN"
            };
            log(&format!(
                "[6/6] cf-warmup    {} http={}  proxy={}",
                icon, r.status, proxy_mask
            ));
        }
        Err(e) => {
            log(&format!(
                "[6/6] cf-warmup    WARN skip ({}) — approve sẽ tự xoay nếu 403  proxy={}",
                e, proxy_mask
            ));
        }
    }
}

/// Confirm UPI payload theo variant.
fn upi_payload_for_variant(variant: &str) -> Value {
    match variant {
        "flow_qr" => json!({"flow": "qr_code"}),
        "qr_code" => json!({"qr_code": {}}),
        "intent" => json!({"intent": "qr_code"}),
        _ => json!({}),
    }
}

#[derive(Debug, Clone)]
pub struct CheckoutResponse {
    pub session_id: String,
    pub publishable_key: String,
    pub checkout_ui_mode: Option<String>,
    pub raw: Value,
}

pub async fn create_chatgpt_checkout(
    client: &HttpClient,
    auth: &UpiAuth,
    proxy: Option<&str>,
) -> Result<CheckoutResponse> {
    let body = json!({
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "IN", "currency": "INR"},
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": false
        },
        "checkout_ui_mode": "custom"
    });
    let auth_header = format!("Bearer {}", auth.access_token);
    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert("Authorization", &auth_header);
    headers.insert("Content-Type", "application/json");
    headers.insert("Accept", "*/*");
    headers.insert("Accept-Language", "en-IN,en;q=0.9");
    headers.insert("Origin", "https://chatgpt.com");
    headers.insert("Referer", "https://chatgpt.com/?promo_campaign=plus-1-month-free");
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("x-openai-target-path", "/backend-api/payments/checkout");
    headers.insert("x-openai-target-route", "/backend-api/payments/checkout");
    headers.insert("OAI-Language", "en-IN");
    if !auth.cookie_header.is_empty() {
        headers.insert("Cookie", &auth.cookie_header);
    }

    let resp = client.post_json(CHATGPT_CHECKOUT_URL, &headers, &body, proxy).await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "checkout HTTP {}: {}",
            resp.status,
            safe_truncate(&resp.body, 300)
        ));
    }
    let data: Value = serde_json::from_str(&resp.body)?;
    let session_id = data
        .get("checkout_session_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("checkout response missing checkout_session_id"))?
        .to_string();
    let publishable_key = data
        .get("publishable_key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("checkout response missing publishable_key"))?
        .to_string();
    let checkout_ui_mode = data
        .get("checkout_ui_mode")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    Ok(CheckoutResponse {
        session_id,
        publishable_key,
        checkout_ui_mode,
        raw: data,
    })
}

pub async fn stripe_init(
    client: &HttpClient,
    session_id: &str,
    publishable_key: &str,
    stripe_js_id: &str,
    proxy: Option<&str>,
) -> Result<Value> {
    let url = STRIPE_INIT_URL_TPL.replace("{id}", session_id);
    let payload = json!({
        "browser_locale": "en-IN",
        "browser_timezone": "Asia/Kolkata",
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1",
                "custom_checkout_manual_approval_1"
            ],
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": "en",
            "is_aggregation_expected": "false"
        },
        "elements_options_client": {
            "saved_payment_method": {
                "enable_save": "auto",
                "enable_redisplay": "auto"
            }
        },
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION
    });
    let form = to_form(&payload);
    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert("Content-Type", "application/x-www-form-urlencoded");
    headers.insert("Accept", "application/json");
    headers.insert("Origin", "https://js.stripe.com");
    headers.insert("Referer", "https://js.stripe.com/");
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("Accept-Language", "en-IN,en;q=0.9");

    let resp = client.post_form(&url, &headers, &form, proxy).await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "stripe init HTTP {}: {}",
            resp.status,
            safe_truncate(&resp.body, 300)
        ));
    }
    let data: Value = serde_json::from_str(&resp.body)?;
    if data.get("init_checksum").is_none() || data.get("config_id").is_none() {
        return Err(anyhow!(
            "stripe init missing init_checksum/config_id; keys present"
        ));
    }
    Ok(data)
}

pub async fn stripe_elements_session(
    client: &HttpClient,
    session_id: &str,
    publishable_key: &str,
    stripe_js_id: &str,
    amount: i64,
    proxy: Option<&str>,
) -> Result<Value> {
    let amount_str = amount.to_string();
    let pubkey = publishable_key.to_string();
    let sver = STRIPE_VERSION.to_string();
    let cs = session_id.to_string();
    let sid = stripe_js_id.to_string();
    let query: Vec<(String, String)> = vec![
        ("client_betas[0]".into(), "custom_checkout_server_updates_1".into()),
        ("client_betas[1]".into(), "custom_checkout_manual_approval_1".into()),
        ("deferred_intent[mode]".into(), "subscription".into()),
        ("deferred_intent[amount]".into(), amount_str),
        ("deferred_intent[currency]".into(), "inr".into()),
        ("deferred_intent[setup_future_usage]".into(), "off_session".into()),
        ("deferred_intent[payment_method_types][0]".into(), "card".into()),
        ("deferred_intent[payment_method_types][1]".into(), "link".into()),
        ("deferred_intent[payment_method_types][2]".into(), "upi".into()),
        ("currency".into(), "inr".into()),
        ("key".into(), pubkey),
        ("_stripe_version".into(), sver),
        ("elements_init_source".into(), "custom_checkout".into()),
        ("referrer_host".into(), "chatgpt.com".into()),
        ("stripe_js_id".into(), sid),
        ("locale".into(), "en".into()),
        ("type".into(), "deferred_intent".into()),
        ("checkout_session_id".into(), cs),
    ];

    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert("Accept", "application/json");
    headers.insert("Origin", "https://js.stripe.com");
    headers.insert("Referer", "https://js.stripe.com/");
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("Accept-Language", "en-IN,en;q=0.9");

    let resp = client
        .get_with_query(STRIPE_ELEMENTS_URL, &headers, &query, proxy)
        .await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "elements/sessions HTTP {}: {}",
            resp.status,
            safe_truncate(&resp.body, 300)
        ));
    }
    let data: Value = serde_json::from_str(&resp.body)?;
    if data.get("session_id").is_none() {
        return Err(anyhow!("elements/sessions missing session_id"));
    }
    Ok(data)
}

#[derive(Debug, Clone)]
pub struct ConfirmAttempt {
    pub variant: String,
    pub http_status: Option<u16>,
    pub ok: bool,
    pub keys: Vec<String>,
    pub error: Option<Value>,
    pub data: Option<Value>,
}

#[allow(clippy::too_many_arguments)]
pub async fn stripe_confirm_upi_qr(
    client: &HttpClient,
    session_id: &str,
    publishable_key: &str,
    stripe_js_id: &str,
    init_data: &Value,
    elements_data: &Value,
    profile: &IndiaProfile,
    email: &str,
    amount: i64,
    variant: &str,
    token_config: Option<&StripeTokenConfig>,
    proxy: Option<&str>,
) -> Result<ConfirmAttempt> {
    let url = STRIPE_CONFIRM_URL_TPL.replace("{id}", session_id);
    let elements_session_id = elements_data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let elements_session_config_id = elements_data
        .get("config_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let init_config_id = init_data
        .get("config_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let ppage_id = init_data
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let init_checksum = init_data
        .get("init_checksum")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("init_data missing init_checksum"))?;

    let (js_checksum, rv_timestamp) = match token_config {
        Some(cfg) => (
            Some(compute_js_checksum(ppage_id, cfg.shift)),
            Some(compute_rv_timestamp(cfg)),
        ),
        None => (None, None),
    };

    let cam = json!({
        "checkout_config_id": init_config_id,
        "checkout_session_id": session_id,
        "client_session_id": stripe_js_id,
        "elements_session_config_id": elements_session_config_id,
        "elements_session_id": elements_session_id,
        "merchant_integration_additional_elements": ["expressCheckout", "payment", "address"],
        "merchant_integration_source": "checkout",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "custom",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    });
    let mut pmd_cam = cam.clone();
    if let Value::Object(ref mut o) = pmd_cam {
        o.insert("merchant_integration_source".into(), json!("elements"));
        o.insert("merchant_integration_version".into(), json!("2021"));
    }

    let payload = json!({
        "_stripe_version": STRIPE_VERSION,
        "client_attribution_metadata": cam,
        "elements_options_client": {
            "saved_payment_method": {"enable_redisplay": "auto", "enable_save": "auto"}
        },
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1",
                "custom_checkout_manual_approval_1"
            ],
            "elements_init_source": "custom_checkout",
            "is_aggregation_expected": "false",
            "locale": "en",
            "referrer_host": "chatgpt.com",
            "session_id": elements_session_id,
            "stripe_js_id": stripe_js_id
        },
        "expected_amount": amount,
        "expected_payment_method_type": "upi",
        "guid": stripe_guid(),
        "init_checksum": init_checksum,
        "js_checksum": js_checksum,
        "rv_timestamp": rv_timestamp,
        "passive_captcha_ekey": Value::Null,
        "passive_captcha_token": Value::Null,
        "key": publishable_key,
        "muid": stripe_guid(),
        "sid": stripe_guid(),
        "payment_method_data": {
            "billing_details": {
                "address": {
                    "city": &profile.city,
                    "country": "IN",
                    "line1": &profile.address_line1,
                    "postal_code": &profile.postal_code,
                    "state": &profile.state,
                },
                "email": email,
                "name": &profile.name,
            },
            "client_attribution_metadata": pmd_cam,
            "payment_user_agent": "stripe.js/e5ebd5e1e6; stripe-js-v3/e5ebd5e1e6; payment-element; deferred-intent",
            "referrer": "https://chatgpt.com",
            "time_on_page": (now_ms() % 100000) as i64,
            "type": "upi",
            "upi": upi_payload_for_variant(variant),
        },
        "return_url": stripe_return_url(session_id),
        "version": "e5ebd5e1e6",
    });

    let form = to_form(&payload);
    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert("Content-Type", "application/x-www-form-urlencoded");
    headers.insert("Accept", "application/json");
    headers.insert("Origin", "https://js.stripe.com");
    headers.insert("Referer", "https://js.stripe.com/");
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("Accept-Language", "en-IN,en;q=0.9");

    let resp = client.post_form(&url, &headers, &form, proxy).await?;
    let data: Value = serde_json::from_str(&resp.body).unwrap_or_else(|_| {
        json!({"_raw": safe_truncate(&resp.body, 1000)})
    });
    let ok = resp.status == 200;
    let keys = collect_keys(&data, 30);
    let error = if !ok || data.get("error").is_some() {
        data.get("error").cloned()
    } else {
        None
    };
    Ok(ConfirmAttempt {
        variant: variant.to_string(),
        http_status: Some(resp.status),
        ok,
        keys,
        error,
        data: if ok { Some(data) } else { None },
    })
}

#[derive(Debug, Clone)]
pub struct RefreshAttempt {
    pub http_status: Option<u16>,
    pub ok: bool,
    pub keys: Vec<String>,
    #[allow(dead_code)]
    pub error: Option<Value>,
    pub error_type: Option<String>,
    pub error_msg: Option<String>,
    pub data: Option<Value>,
}

pub async fn stripe_payment_page_refresh(
    client: &HttpClient,
    session_id: &str,
    publishable_key: &str,
    stripe_js_id: &str,
    elements_data: &Value,
    proxy: Option<&str>,
) -> Result<RefreshAttempt> {
    let url = STRIPE_PAGE_URL_TPL.replace("{id}", session_id);
    let elements_session_id = elements_data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let payload = json!({
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1",
                "custom_checkout_manual_approval_1"
            ],
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": "en",
            "is_aggregation_expected": "false",
            "session_id": elements_session_id
        },
        "elements_options_client": {
            "saved_payment_method": {"enable_save": "auto", "enable_redisplay": "auto"}
        },
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION
    });
    let query = to_form(&payload);

    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert("Accept", "application/json");
    headers.insert("Origin", "https://js.stripe.com");
    headers.insert("Referer", "https://js.stripe.com/");
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("Accept-Language", "en-IN,en;q=0.9");

    let resp = client.get_with_query(&url, &headers, &query, proxy).await?;
    let data: Value = serde_json::from_str(&resp.body).unwrap_or_else(|_| {
        json!({"_raw": safe_truncate(&resp.body, 1000)})
    });
    let ok = resp.status == 200;
    let keys = collect_keys(&data, 30);
    let error = data.get("error").cloned();
    Ok(RefreshAttempt {
        http_status: Some(resp.status),
        ok,
        keys,
        error,
        error_type: None,
        error_msg: None,
        data: if ok { Some(data) } else { None },
    })
}

#[derive(Debug, Clone)]
pub struct ApproveAttempt {
    pub http_status: Option<u16>,
    pub ok: bool,
    pub result: Option<String>,
    pub keys: Vec<String>,
    pub error_type: Option<String>,
    pub error: Option<String>,
    pub data: Option<Value>,
}

pub async fn chatgpt_approve_checkout(
    client: &HttpClient,
    auth: &UpiAuth,
    session_id: &str,
    proxy: Option<&str>,
) -> Result<ApproveAttempt> {
    let body = json!({
        "checkout_session_id": session_id,
        "processor_entity": "openai_llc"
    });
    let auth_header = format!("Bearer {}", auth.access_token);
    let referer = format!("https://chatgpt.com/checkout/openai_llc/{}", session_id);
    let mut headers: HashMap<&str, &str> = HashMap::new();
    headers.insert("Authorization", &auth_header);
    headers.insert("Content-Type", "application/json");
    headers.insert("Accept", "*/*");
    headers.insert("Accept-Language", "en-IN,en;q=0.9");
    headers.insert("Origin", "https://chatgpt.com");
    headers.insert("Referer", &referer);
    headers.insert("User-Agent", WINDOWS_USER_AGENT);
    headers.insert("sec-ch-ua", SEC_CH_UA);
    headers.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    headers.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    headers.insert("x-openai-target-path", "/backend-api/payments/checkout/approve");
    headers.insert("x-openai-target-route", "/backend-api/payments/checkout/approve");
    headers.insert("OAI-Language", "en-IN");
    if !auth.cookie_header.is_empty() {
        headers.insert("Cookie", &auth.cookie_header);
    }

    let resp = client
        .post_json(CHATGPT_APPROVE_URL, &headers, &body, proxy)
        .await?;
    let data: Value = serde_json::from_str(&resp.body).unwrap_or_else(|_| {
        json!({"_raw": safe_truncate(&resp.body, 1000)})
    });
    let result = data
        .get("result")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let ok = resp.status == 200 && result.as_deref() == Some("approved");
    let keys = collect_keys(&data, 30);

    // Phân loại lỗi non-200: phân biệt Cloudflare edge block vs OpenAI app
    // error để debug/quản lý proxy. Body được trim còn 200 chars để tránh
    // ngập log nhưng đủ để nhận dạng (cf-ray, error JSON detail...).
    let (error_type, error) = if resp.status != 200 {
        let body_lower = resp.body.to_lowercase();
        let is_cf = body_lower.contains("cf-ray")
            || body_lower.contains("cloudflare")
            || body_lower.contains("just a moment")
            || body_lower.contains("attention required")
            || body_lower.contains("<!doctype html");
        let etype = if is_cf {
            format!("CFBlock{}", resp.status)
        } else {
            format!("HTTP{}", resp.status)
        };
        let body_short: String = resp.body.chars().take(200).collect::<String>()
            .replace('\n', " ")
            .replace('\r', " ");
        (Some(etype), Some(body_short))
    } else {
        (None, None)
    };

    Ok(ApproveAttempt {
        http_status: Some(resp.status),
        ok,
        result,
        keys,
        error_type,
        error,
        data: if resp.status == 200 { Some(data) } else { None },
    })
}

fn collect_keys(v: &Value, max: usize) -> Vec<String> {
    if let Value::Object(o) = v {
        o.keys().take(max).cloned().collect()
    } else {
        Vec::new()
    }
}

/// Extract `amount` from init_data (Python `_extract_amount`).
pub fn extract_amount(init_data: &Value) -> i64 {
    if let Some(eo) = init_data.get("elements_options").and_then(|v| v.as_object()) {
        if let Some(a) = eo.get("amount").and_then(|v| v.as_i64()) {
            return a;
        }
    }
    if let Some(ts) = init_data.get("total_summary").and_then(|v| v.as_object()) {
        for k in ["due", "total"] {
            if let Some(a) = ts.get(k).and_then(|v| v.as_i64()) {
                return a;
            }
        }
    }
    if let Some(inv) = init_data.get("invoice").and_then(|v| v.as_object()) {
        for k in ["amount_due", "total"] {
            if let Some(a) = inv.get(k).and_then(|v| v.as_i64()) {
                return a;
            }
        }
    }
    init_data
        .get("amount_total")
        .and_then(|v| v.as_i64())
        .unwrap_or(0)
}
