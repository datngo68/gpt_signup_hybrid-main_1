//! Fetch + parse Stripe `js.stripe.com/v3/` entry → resolve fingerprinted
//! `custom-checkout-<hash>.js` → extract `StripeTokenConfig`.
//!
//! Port từ `stripe_token.py::fetch_bundles_live` + `extract_config_live`.
//! Cache theo SHA256 entry source.

use crate::http::HttpClient;
use crate::stripe_token::{extract_config, StripeTokenConfig};
use crate::user_agent::{
    SEC_CH_UA, SEC_CH_UA_MOBILE, SEC_CH_UA_PLATFORM, WINDOWS_USER_AGENT,
};
use anyhow::{anyhow, Result};
use regex::Regex;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::PathBuf;

pub struct BundleCache {
    pub root: PathBuf,
}

impl BundleCache {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn dir_for(&self, hash: &str) -> PathBuf {
        self.root.join(&hash[..16])
    }

    pub async fn read_pair(&self, hash: &str) -> Option<(String, String)> {
        let d = self.dir_for(hash);
        let cc = d.join("custom_checkout.js");
        let entry = d.join("entry.js");
        if !cc.exists() || !entry.exists() {
            return None;
        }
        let cc_src = tokio::fs::read_to_string(&cc).await.ok()?;
        let entry_src = tokio::fs::read_to_string(&entry).await.ok()?;
        Some((cc_src, entry_src))
    }

    pub async fn write_pair(&self, hash: &str, cc_src: &str, entry_src: &str) -> Result<()> {
        let d = self.dir_for(hash);
        tokio::fs::create_dir_all(&d).await?;
        tokio::fs::write(d.join("custom_checkout.js"), cc_src).await?;
        tokio::fs::write(d.join("entry.js"), entry_src).await?;
        Ok(())
    }
}

/// Common Chrome headers cho Stripe JS request.
fn common_headers() -> HashMap<&'static str, &'static str> {
    let mut h = HashMap::new();
    h.insert("User-Agent", WINDOWS_USER_AGENT);
    h.insert("sec-ch-ua", SEC_CH_UA);
    h.insert("sec-ch-ua-mobile", SEC_CH_UA_MOBILE);
    h.insert("sec-ch-ua-platform", SEC_CH_UA_PLATFORM);
    h.insert("Accept", "*/*");
    h.insert("Accept-Language", "en-IN,en;q=0.9");
    h
}

pub async fn fetch_bundles_live(
    client: &HttpClient,
    cache: &BundleCache,
) -> Result<(String, String)> {
    let mut entry_headers = common_headers();
    entry_headers.insert("Referer", "https://chatgpt.com/");

    let entry_resp = client
        .get_text("https://js.stripe.com/v3/", &entry_headers, None)
        .await?;
    if entry_resp.status != 200 {
        return Err(anyhow!(
            "entry HTTP {}: {}",
            entry_resp.status,
            entry_resp.body.chars().take(200).collect::<String>()
        ));
    }
    let entry_src = entry_resp.body;
    let entry_hash = {
        let mut h = Sha256::new();
        h.update(entry_src.as_bytes());
        format!("{:x}", h.finalize())
    };

    if let Some((cc, entry)) = cache.read_pair(&entry_hash).await {
        return Ok((cc, entry));
    }

    // Parse webpack chunk maps
    let mut chunk_names: HashMap<u64, String> = HashMap::new();
    let mut chunk_hashes: HashMap<u64, String> = HashMap::new();

    let name_block_re = Regex::new(r#""fingerprinted/js/"[^}]*?\{([^}]+)\}"#)?;
    if let Some(m) = name_block_re.captures(&entry_src) {
        let name_re = Regex::new(r#"(\d+):"([a-z][a-zA-Z0-9_-]+)""#)?;
        for cap in name_re.captures_iter(&m[1]) {
            if let (Ok(id), name) = (cap[1].parse::<u64>(), cap[2].to_string()) {
                chunk_names.insert(id, name);
            }
        }
    }

    let hash_block_re = Regex::new(r#"\{(?:\d+:"[a-f0-9]{20,}",?){3,40}\}"#)?;
    if let Some(m) = hash_block_re.find(&entry_src) {
        let hash_re = Regex::new(r#"(\d+):"([a-f0-9]{20,})""#)?;
        for cap in hash_re.captures_iter(m.as_str()) {
            if let (Ok(id), hash) = (cap[1].parse::<u64>(), cap[2].to_string()) {
                chunk_hashes.insert(id, hash);
            }
        }
    }

    if chunk_names.is_empty() || chunk_hashes.is_empty() {
        return Err(anyhow!(
            "không parse được webpack chunk map (names={}, hashes={})",
            chunk_names.len(),
            chunk_hashes.len()
        ));
    }

    let cc_id = chunk_names
        .iter()
        .find(|(_, n)| n.as_str() == "custom-checkout")
        .map(|(id, _)| *id)
        .ok_or_else(|| anyhow!("không thấy chunk 'custom-checkout' trong map"))?;
    let cc_hash = chunk_hashes
        .get(&cc_id)
        .ok_or_else(|| anyhow!("không có hash cho chunk {} (custom-checkout)", cc_id))?;

    let cc_url = format!(
        "https://js.stripe.com/v3/fingerprinted/js/custom-checkout-{}.js",
        cc_hash
    );

    let mut sub_headers = common_headers();
    sub_headers.insert("Referer", "https://js.stripe.com/v3/");
    sub_headers.insert("Sec-Fetch-Dest", "script");
    sub_headers.insert("Sec-Fetch-Mode", "no-cors");
    sub_headers.insert("Sec-Fetch-Site", "same-origin");

    let cc_resp = client.get_text(&cc_url, &sub_headers, None).await?;
    if cc_resp.status != 200 {
        return Err(anyhow!(
            "custom_checkout HTTP {}: {}",
            cc_resp.status,
            cc_resp.body.chars().take(200).collect::<String>()
        ));
    }
    let cc_src = cc_resp.body;

    cache.write_pair(&entry_hash, &cc_src, &entry_src).await.ok();
    Ok((cc_src, entry_src))
}

pub async fn extract_config_live(
    client: &HttpClient,
    cache: &BundleCache,
) -> Result<StripeTokenConfig> {
    let (cc_src, entry_src) = fetch_bundles_live(client, cache).await?;
    extract_config(&cc_src, &[entry_src])
}
