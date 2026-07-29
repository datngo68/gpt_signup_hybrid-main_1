//! Windows Chrome 145 persona — port từ `user_agent_profile.py`.
//!
//! Single source of truth cho UA + sec-ch-ua headers. Đồng bộ với Phase
//! Python để Stripe/ChatGPT thấy cùng device persona.

#[allow(dead_code)]
pub const CHROME_MAJOR: &str = "145";
#[allow(dead_code)]
pub const CHROME_FULL: &str = "145.0.0.0";

pub const WINDOWS_USER_AGENT: &str = concat!(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ",
    "AppleWebKit/537.36 (KHTML, like Gecko) ",
    "Chrome/145.0.0.0 Safari/537.36",
);

pub const SEC_CH_UA: &str =
    r#""Chromium";v="145", "Google Chrome";v="145", "Not_A Brand";v="24""#;

pub const SEC_CH_UA_MOBILE: &str = "?0";
pub const SEC_CH_UA_PLATFORM: &str = r#""Windows""#;
#[allow(dead_code)]
pub const SEC_CH_UA_PLATFORM_VERSION: &str = r#""15.0.0""#;
