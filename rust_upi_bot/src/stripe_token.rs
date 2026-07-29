//! Stripe `js_checksum` + `rv_timestamp` token generator — port từ `stripe_token.py`.
//!
//! Reverse-engineered thuật toán Stripe (verified 10/10 PASS với HAR thật):
//!
//!   caesar_shift(s, n) = char-by-char (ord - 32 + n) % 95 + 32
//!   stripe_encode(s)   = url_encode(base64(xor5(s + pad_to_3(' '))))
//!   js_checksum        = caesar_shift(stripe_encode(JSON.stringify({id})), 11)
//!   rv_timestamp       = caesar_shift(stripe_encode(JSON.stringify({rvTs, rv, sv})), 11)
//!
//! Constants (rvTs/rv/sv) extract live từ Stripe `custom_checkout.js` bundle
//! qua pattern match — anti-fragile khi obfuscation đổi.

use anyhow::{anyhow, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS};
use regex::Regex;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// URL-encode set khớp Python `urllib.parse.quote(s, safe="-_.!~*'()")`.
/// Mặc định Rust `percent-encoding` chỉ encode CONTROLS — phải mở rộng.
const STRIPE_QUOTE_SET: &AsciiSet = &CONTROLS
    .add(b' ').add(b'"').add(b'#').add(b'$').add(b'%').add(b'&').add(b'+')
    .add(b',').add(b'/').add(b':').add(b';').add(b'<').add(b'=').add(b'>')
    .add(b'?').add(b'@').add(b'[').add(b'\\').add(b']').add(b'^').add(b'`')
    .add(b'{').add(b'|').add(b'}');

/// Caesar shift trên ASCII printable [32..127), wrap %95.
pub fn caesar_shift(s: &str, n: i32) -> String {
    s.chars()
        .map(|c| {
            let code = c as i32;
            let shifted = (((code - 32 + n) % 95 + 95) % 95) + 32;
            char::from_u32(shifted as u32).unwrap_or(c)
        })
        .collect()
}

/// Stripe encode: pad space đến mod 3 (luôn 1..3) → XOR-5 → base64 → url-encode.
///
/// Quirk: pad = 3 - len % 3 (KHÔNG mod lại) → khi len%3==0 vẫn pad 3 spaces.
pub fn stripe_encode(s: &str) -> String {
    let pad = 3 - (s.len() % 3);
    let mut padded: Vec<u8> = s.bytes().collect();
    padded.extend(std::iter::repeat(b' ').take(pad));
    let xored: Vec<u8> = padded.iter().map(|b| 5u8 ^ b).collect();
    let b64 = STANDARD.encode(&xored);
    utf8_percent_encode(&b64, STRIPE_QUOTE_SET).to_string()
}

/// JSON stringify minified — preserve key insertion order (như JS).
fn js_stringify_id(id: &str) -> String {
    // {"id":"<value>"} — escape JSON
    let mut s = String::from("{\"id\":");
    push_json_string(&mut s, id);
    s.push('}');
    s
}

fn js_stringify_rv(rv_ts: &str, rv: &str, sv: &str) -> String {
    let mut s = String::from("{\"rvTs\":");
    push_json_string(&mut s, rv_ts);
    s.push_str(",\"rv\":");
    push_json_string(&mut s, rv);
    s.push_str(",\"sv\":");
    push_json_string(&mut s, sv);
    s.push('}');
    s
}

fn push_json_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StripeTokenConfig {
    pub bundle_hash: String,
    pub shift: i32,
    pub rv_ts: String,
    pub rv: String,
    pub sv: String,
}

pub fn compute_js_checksum(ppage_id: &str, shift: i32) -> String {
    let payload = js_stringify_id(ppage_id);
    caesar_shift(&stripe_encode(&payload), shift)
}

pub fn compute_rv_timestamp(cfg: &StripeTokenConfig) -> String {
    let payload = js_stringify_rv(&cfg.rv_ts, &cfg.rv, &cfg.sv);
    caesar_shift(&stripe_encode(&payload), cfg.shift)
}

// ─────────────────────────────────────────────────────────────────────
// Bundle extraction — pattern match thuật toán, không bám tên var/ID.
// ─────────────────────────────────────────────────────────────────────

/// Extract config từ Stripe `custom_checkout.js` bundle + (optional) entry
/// bundle (chứa module 114 constants).
pub fn extract_config(
    bundle_source: &str,
    fallback_sources: &[String],
) -> Result<StripeTokenConfig> {
    let mut hasher = Sha256::new();
    hasher.update(bundle_source.as_bytes());
    let bundle_hash = hex_encode(&hasher.finalize());

    // 1. Caesar shift function — verify pattern tồn tại
    let caesar_re = Regex::new(
        r"(?s)\b[a-zA-Z_$][\w$]{0,3}\s*=\s*function\s*\(\s*[a-zA-Z_$][\w$]{0,3}\s*,\s*[a-zA-Z_$][\w$]{0,3}\s*\)\s*\{[^{}]*?charCodeAt\([^)]*?\)\s*-\s*32\s*\+\s*[a-zA-Z_$][\w$]{0,3}\s*\)\s*%\s*95\s*\+\s*32[^{}]*?\}",
    )?;
    if !caesar_re.is_match(bundle_source) {
        return Err(anyhow!(
            "Caesar shift function pattern không tìm thấy — Stripe đã đổi thuật toán encode"
        ));
    }

    // 2. js_checksum builder → extract shift
    let js_re = Regex::new(
        r"\b(?P<fn>[a-zA-Z_$][\w$]{0,3})\s*\(\s*\(\s*0\s*,\s*(?P<encmod>[a-zA-Z_$][\w$]{0,3})\s*\.\s*(?P<encfn>[a-zA-Z_$][\w$]{0,3})\s*\)\s*\(\s*JSON\s*\.\s*stringify\s*\(\s*\{\s*id\s*:\s*[a-zA-Z_$][\w$]*\s*\}\s*\)\s*\)\s*,\s*(?P<shift>\d+)\s*\)",
    )?;
    let js_match = js_re
        .captures(bundle_source)
        .ok_or_else(|| anyhow!("js_checksum builder pattern không tìm thấy"))?;
    let shift: i32 = js_match["shift"].parse()?;

    // 3. rv_timestamp builder
    let rv_re = Regex::new(
        r"rv_timestamp\s*:\s*[a-zA-Z_$][\w$]{0,3}\s*\(\s*\(\s*0\s*,\s*[a-zA-Z_$][\w$]{0,3}\s*\.\s*[a-zA-Z_$][\w$]{0,3}\s*\)\s*\(\s*JSON\s*\.\s*stringify\s*\(\s*\{(?P<keys>[^}]+)\}\s*\)\s*\)\s*,\s*(?P<shift>\d+)\s*\)",
    )?;
    let rv_match = rv_re
        .captures(bundle_source)
        .ok_or_else(|| anyhow!("rv_timestamp builder pattern không tìm thấy"))?;
    let rv_shift: i32 = rv_match["shift"].parse()?;
    if rv_shift != shift {
        return Err(anyhow!(
            "shift mismatch js_checksum={shift} vs rv_timestamp={rv_shift}"
        ));
    }
    let keys_literal = &rv_match["keys"];

    // Map rv/sv/rvTs key → module member
    let member_re = Regex::new(
        r"(\w+)\s*:\s*([a-zA-Z_$][\w$]*)\s*\.\s*([a-zA-Z_$][\w$]*)",
    )?;
    let member_refs: Vec<(String, String, String)> = member_re
        .captures_iter(keys_literal)
        .map(|c| (c[1].to_string(), c[2].to_string(), c[3].to_string()))
        .collect();
    if member_refs.len() != 3 {
        return Err(anyhow!(
            "rv_timestamp keys layout đã đổi — expect 3 refs, got {}",
            member_refs.len()
        ));
    }

    // 4. Resolve module ID cho constants module trong scope quanh rv_match
    let rv_start = rv_match.get(0).unwrap().start();
    let scope_start = rv_start.saturating_sub(4000);
    let scope_end = (rv_start + 4000).min(bundle_source.len());
    let rv_scope = &bundle_source[scope_start..scope_end];

    let constants_local = &member_refs[0].1;
    let webpack_re = Regex::new(
        r"\b(?P<lhs>[a-zA-Z_$][\w$]{0,3})\s*=\s*[a-zA-Z_$][\w$]{0,3}\s*\(\s*(?P<id>\d+)\s*\)",
    )?;
    let mut constants_module_id: Option<u32> = None;
    for cap in webpack_re.captures_iter(rv_scope) {
        if &cap["lhs"] == constants_local {
            constants_module_id = Some(cap["id"].parse()?);
            break;
        }
    }
    let module_id = constants_module_id.ok_or_else(|| {
        anyhow!("không resolve được module ID cho constants local {}", constants_local)
    })?;

    // 5. Extract module body — try bundle chính trước, fallback các bundle khác
    let mut mod_body = extract_webpack_module(bundle_source, module_id);
    if mod_body.is_empty() {
        for fb in fallback_sources {
            mod_body = extract_webpack_module(fb, module_id);
            if !mod_body.is_empty() {
                break;
            }
        }
    }
    if mod_body.is_empty() {
        return Err(anyhow!(
            "không tìm thấy body module {} trong bundle chính hoặc {} fallback",
            module_id,
            fallback_sources.len()
        ));
    }

    // 6. Extract constants {sK, dG, QJ}
    let constants = extract_constants_from_module(&mod_body);
    let mut key_to_member = std::collections::HashMap::new();
    for (key, _, member) in &member_refs {
        key_to_member.insert(key.clone(), member.clone());
    }

    let rv_ts_member = key_to_member
        .get("rvTs")
        .ok_or_else(|| anyhow!("missing rvTs in member_refs"))?;
    let rv_member = key_to_member
        .get("rv")
        .ok_or_else(|| anyhow!("missing rv in member_refs"))?;
    let sv_member = key_to_member
        .get("sv")
        .ok_or_else(|| anyhow!("missing sv in member_refs"))?;

    let rv_ts = constants
        .get(rv_ts_member)
        .ok_or_else(|| anyhow!("constants thiếu rvTs ({})", rv_ts_member))?
        .clone();
    let rv = constants
        .get(rv_member)
        .ok_or_else(|| anyhow!("constants thiếu rv ({})", rv_member))?
        .clone();
    let sv = constants
        .get(sv_member)
        .ok_or_else(|| anyhow!("constants thiếu sv ({})", sv_member))?
        .clone();

    Ok(StripeTokenConfig { bundle_hash, shift, rv_ts, rv, sv })
}

/// Extract body của webpack module ID — pattern: `<id>:function(...){...}` hoặc
/// `<id>:(args)=>{...}`.
fn extract_webpack_module(body: &str, mod_id: u32) -> String {
    let pattern = Regex::new(&format!(r"[\s,{{(]({})\s*:\s*", mod_id)).unwrap();
    let sig_re = Regex::new(
        r"\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>|[a-zA-Z_$][\w$]*\s*=>)\s*\{",
    )
    .unwrap();

    for m in pattern.find_iter(body) {
        let after = &body[m.end()..];
        let mut preview_end = (200).min(after.len());
        // Lùi về char boundary — bundle JS có thể chứa ký tự đa byte, slice byte
        // giữa ký tự sẽ panic (release `panic=abort` → chết cả tiến trình).
        while preview_end > 0 && !after.is_char_boundary(preview_end) {
            preview_end -= 1;
        }
        let preview = &after[..preview_end];
        if let Some(sig) = sig_re.find(preview) {
            let brace_open = m.end() + sig.end() - 1;
            if let Some(brace_close) = balanced_brace(body, brace_open) {
                return body[m.start()..=brace_close].to_string();
            }
        }
    }
    String::new()
}

fn balanced_brace(body: &str, open_pos: usize) -> Option<usize> {
    let bytes = body.as_bytes();
    let mut depth = 0i32;
    let mut in_str = false;
    let mut str_ch: u8 = 0;
    let mut i = open_pos;
    while i < bytes.len() {
        let c = bytes[i];
        if in_str {
            if c == b'\\' {
                i += 2;
                continue;
            }
            if c == str_ch {
                in_str = false;
            }
        } else {
            match c {
                b'\'' | b'"' | b'`' => {
                    in_str = true;
                    str_ch = c;
                }
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        return Some(i);
                    }
                }
                _ => {}
            }
        }
        i += 1;
    }
    None
}

fn extract_constants_from_module(mod_body: &str) -> std::collections::HashMap<String, String> {
    // export_map: tên export → local var, vd `QJ → a`
    let export_re = Regex::new(
        r"([a-zA-Z_$][\w$]{0,3})\s*:\s*function\s*\(\s*\)\s*\{\s*return\s+([a-zA-Z_$][\w$]{0,3})\s*\}",
    )
    .unwrap();
    let mut export_map: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    for cap in export_re.captures_iter(mod_body) {
        export_map.insert(cap[1].to_string(), cap[2].to_string());
    }

    // var_values: local var → string literal (cho phép /*! ... */ comment chèn)
    let var_re = Regex::new(
        r#"\b([a-zA-Z_$][\w$]{0,3})\s*=\s*(?:/\*[^*]*(?:\*(?:[^/])[^*]*)*\*/\s*)?"([^"]*)""#,
    )
    .unwrap();
    let mut var_values: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    for cap in var_re.captures_iter(mod_body) {
        let name = cap[1].to_string();
        if !var_values.contains_key(&name) {
            var_values.insert(name, cap[2].to_string());
        }
    }

    let mut out = std::collections::HashMap::new();
    for (export, local) in &export_map {
        if let Some(val) = var_values.get(local) {
            out.insert(export.clone(), val.clone());
        }
    }
    out
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}


#[cfg(test)]
mod tests {
    //! Parity test với Python `gpt_signup_hybrid.stripe_token`. Reference
    //! values sinh từ `test/check_stripe_token_parity.py`.

    use super::*;

    #[test]
    fn parity_caesar_shift() {
        assert_eq!(caesar_shift("Hello", 11), "Spwwz");
        assert_eq!(caesar_shift("abc XYZ 123", 11), "lmn+cde+<=>");
        assert_eq!(caesar_shift("!@#$%", 5), "&E()*");
    }

    #[test]
    fn parity_stripe_encode() {
        assert_eq!(stripe_encode("test"), "cWB2cSUl");
        assert_eq!(stripe_encode(r#"{"id":"abc"}"#), "fidsYSc%2FJ2RnZid4JSUl");
        assert_eq!(stripe_encode("hello world!"), "bWBpaWolcmp3aWEkJSUl");
    }

    #[test]
    fn parity_js_checksum() {
        assert_eq!(
            compute_js_checksum("test_ppage_id_abc", 11),
            "qto~d^n0=QU>QroyQlocavdxMlmRQleRoxU>rw"
        );
        assert_eq!(
            compute_js_checksum("pp_xyz_123", 11),
            "qto~d^n0=QU>a<by<Cq<z;Y&dypN`w"
        );
    }

    #[test]
    fn parity_rv_timestamp() {
        let cfg = StripeTokenConfig {
            bundle_hash: "dummy".into(),
            shift: 11,
            rv_ts: "2024-01-01 00:00:00 -0000".into(),
            rv: "e5ebd5e1e6abc123".into(),
            sv: "3c7ef39815def456".into(),
        };
        assert_eq!(
            compute_rv_timestamp(&cfg),
            r#"qto>n<Q=U&CyY&`>X^r<YNr<YN`<Y_C<Y_C<Y^`zY_`<Y^n{U>o&U&CydOMre=P#dO]rX=]yeu\>Ytn{U>e&U&CyYxd%dRX=[O;;XRQrd&P#X%o?U^`w"#
        );
    }
}
