//! Public types — port từ `upi_runner.py::UpiQrResult`.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct UpiQrResult {
    pub ok: bool,
    pub email: String,
    pub amount: i64,
    pub return_url: String,
    pub checkout_session: String,
    pub qr_path: Option<String>,
    pub qr_source: Option<String>,
    pub qr_source_url: Option<String>,
    pub qr_reason: Option<String>,
    pub qr_expires_at: Option<i64>,
    /// hosted_instructions_url của UPI (`https://payments.stripe.com/upi/instructions/...`)
    /// — trang QR + nút mở app UPI hosted bởi Stripe. Đây là "payment link"
    /// đúng dạng user cần. Port từ Python `UpiQrResult.payment_link`.
    pub payment_link: Option<String>,
    pub has_upi_uri: bool,
    pub has_qr_image_url: bool,
    pub confirm_attempts: Vec<ConfirmAttemptSummary>,
    pub approve_attempts: Vec<ApproveAttemptSummary>,
    pub page_refresh_attempts: Vec<RefreshAttemptSummary>,
    pub backend_exception_count: u32,
    pub restart_count: u32,
    pub error: Option<String>,
    pub elapsed_seconds: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ConfirmAttemptSummary {
    pub variant: String,
    pub phase: u32,
    pub http_status: Option<u16>,
    pub ok: bool,
    pub keys: Vec<String>,
    pub error: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ApproveAttemptSummary {
    pub variant: Option<String>,
    pub attempt: u32,
    pub phase: u32,
    pub proxy: String,
    pub http_status: Option<u16>,
    pub ok: bool,
    pub result: Option<String>,
    pub error_type: Option<String>,
    pub error: Option<String>,
    pub keys: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RefreshAttemptSummary {
    pub attempt: u32,
    pub proxy: String,
    pub http_status: Option<u16>,
    pub ok: bool,
    pub error_type: Option<String>,
    pub error: Option<String>,
    pub keys: Vec<String>,
}

/// Auth artifacts passed in từ session.json.
#[derive(Debug, Clone)]
pub struct UpiAuth {
    #[allow(dead_code)]
    pub email: String,
    pub access_token: String,
    /// Cookie header string `name=value; name=value` cho chatgpt.com.
    /// Empty nếu session.json không có cookies (Bearer token vẫn đủ cho API).
    pub cookie_header: String,
}

#[derive(thiserror::Error, Debug)]
#[allow(dead_code)]
pub enum UpiError {
    #[error("invalid params: {0}")]
    InvalidParams(String),
    #[error("checkout fail: {0}")]
    Checkout(String),
    #[error("stripe init fail: {0}")]
    StripeInit(String),
    #[error("stripe elements fail: {0}")]
    StripeElements(String),
    #[error("network: {0}")]
    Network(String),
    #[error("other: {0}")]
    Other(#[from] anyhow::Error),
}
