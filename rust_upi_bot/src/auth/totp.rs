//! TOTP 2FA — port từ `totp_helper.py`.
//!
//! OpenAI MFA secret là base32 string (chuẩn Google Authenticator).
//! Thuật toán: TOTP-SHA1(secret, T=floor(unix_time/30), digits=6).

use anyhow::{anyhow, Result};
use hmac::{Hmac, Mac};
use sha1::Sha1;

type HmacSha1 = Hmac<Sha1>;

const STEP_SECONDS: u64 = 30;
const DIGITS: u32 = 6;

/// Chuẩn hoá secret: bỏ space/dash, uppercase, reject ký tự không phải base32.
/// Mirror `totp_helper.normalize_secret`.
pub fn normalize_secret(raw: &str) -> Result<String> {
    let s: String = raw
        .chars()
        .filter(|c| !c.is_whitespace() && *c != '-')
        .collect::<String>()
        .to_uppercase();
    if s.is_empty() {
        return Err(anyhow!("secret rỗng"));
    }
    // RFC4648 base32 alphabet (A-Z, 2-7) + optional padding '='.
    let core = s.trim_end_matches('=');
    if core.is_empty()
        || !core
            .chars()
            .all(|c| c.is_ascii_uppercase() || ('2'..='7').contains(&c))
    {
        return Err(anyhow!("secret chứa ký tự không phải base32: {:?}", s));
    }
    Ok(s)
}

/// Generate 6-digit TOTP code tại unix timestamp cho trước.
pub fn generate_code_at(secret: &str, unix_time: u64) -> Result<String> {
    let norm = normalize_secret(secret)?;
    let core = norm.trim_end_matches('=');
    let key = data_encoding::BASE32_NOPAD
        .decode(core.as_bytes())
        .map_err(|e| anyhow!("base32 decode fail: {}", e))?;
    if key.is_empty() {
        return Err(anyhow!("secret decode rỗng"));
    }

    let counter = unix_time / STEP_SECONDS;
    let msg = counter.to_be_bytes();

    let mut mac =
        HmacSha1::new_from_slice(&key).map_err(|e| anyhow!("hmac key invalid: {}", e))?;
    mac.update(&msg);
    let digest = mac.finalize().into_bytes();

    // Dynamic truncation (RFC 4226).
    let offset = (digest[digest.len() - 1] & 0x0f) as usize;
    let bin = ((u32::from(digest[offset]) & 0x7f) << 24)
        | (u32::from(digest[offset + 1]) << 16)
        | (u32::from(digest[offset + 2]) << 8)
        | u32::from(digest[offset + 3]);
    let modulo = 10u32.pow(DIGITS);
    Ok(format!("{:0width$}", bin % modulo, width = DIGITS as usize))
}

/// Generate code cho thời điểm hiện tại.
pub fn now_code(secret: &str) -> Result<String> {
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| anyhow!("system clock before epoch: {}", e))?
        .as_secs();
    generate_code_at(secret, t)
}

/// Generate code hiện tại + số giây còn lại trong cửa sổ 30s (cho hiển thị
/// đếm ngược). Trả `(code, secs_left)` với `secs_left` ∈ [1, 30].
pub fn now_code_with_ttl(secret: &str) -> Result<(String, u64)> {
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| anyhow!("system clock before epoch: {}", e))?
        .as_secs();
    let code = generate_code_at(secret, t)?;
    let secs_left = STEP_SECONDS - (t % STEP_SECONDS);
    Ok((code, secs_left))
}

#[cfg(test)]
mod tests {
    use super::*;

    // RFC 6238 test vector (SHA1, secret = "12345678901234567890" ASCII).
    // Base32 của ASCII secret đó = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ".
    #[test]
    fn rfc6238_vector() {
        let secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";
        // T = 59 → counter 1 → code 94287082 → 6 digits 287082.
        assert_eq!(generate_code_at(secret, 59).unwrap(), "287082");
        // T = 1111111109 → 081804.
        assert_eq!(generate_code_at(secret, 1111111109).unwrap(), "081804");
    }

    #[test]
    fn normalize_strips_and_uppercases() {
        assert_eq!(
            normalize_secret("jbsw y3dp-ehpk 3pxp").unwrap(),
            "JBSWY3DPEHPK3PXP"
        );
        assert!(normalize_secret("not!base32").is_err());
        assert!(normalize_secret("  ").is_err());
    }
}
