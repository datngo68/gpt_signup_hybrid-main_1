//! HTTP client riêng cho login flow — cookie jar thủ công + redirect tắt.
//!
//! Khác với `http::HttpClient` (dùng cho UPI flow, cookie_store dùng chung mọi
//! job): login cần cookie jar CÔ LẬP per-account + redirect tắt để bắt
//! `Set-Cookie` ở TỪNG hop (NextAuth set `__Secure-next-auth.session-token` ở
//! response 302 trung gian — nếu để reqwest auto-follow sẽ mất cookie khỏi jar).
//!
//! Port hành vi `curl_cffi.Session` mà `request_phase.py` dựa vào:
//!   - giữ cookie xuyên suốt theo domain matching,
//!   - đọc cookie theo tên (`oai-did`, session-token + chunk `.0/.1`),
//!   - export toàn bộ jar ra schema `__cookies`.

use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use std::time::Duration;
use url::Url;
use wreq::header::{HeaderMap, HeaderName, HeaderValue, COOKIE, LOCATION, SET_COOKIE};
use wreq::{Client, Method, Proxy};
use wreq_util::Emulation;

/// 1 cookie trong jar.
#[derive(Debug, Clone)]
struct CookieEntry {
    name: String,
    value: String,
    /// Domain KHÔNG có dấu '.' đầu (vd `chatgpt.com`, `openai.com`).
    domain: String,
    /// host-only = cookie không có attribute Domain (chỉ match đúng host set nó).
    host_only: bool,
    path: String,
    secure: bool,
    http_only: bool,
    expires: i64,
}

#[derive(Default)]
pub struct CookieJar {
    items: Vec<CookieEntry>,
}

impl CookieJar {
    /// Parse 1 header `Set-Cookie` và upsert vào jar.
    fn store(&mut self, set_cookie: &str, request_host: &str) {
        let mut parts = set_cookie.split(';');
        let Some(nv) = parts.next() else { return };
        let nv = nv.trim();
        let Some(eq) = nv.find('=') else { return };
        let name = nv[..eq].trim().to_string();
        let value = nv[eq + 1..].trim().to_string();
        if name.is_empty() {
            return;
        }

        let mut domain = request_host.trim_start_matches('.').to_lowercase();
        let mut host_only = true;
        let mut path = "/".to_string();
        let mut secure = false;
        let mut http_only = false;
        let mut expires: i64 = -1;

        for attr in parts {
            let attr = attr.trim();
            let (k, v) = match attr.find('=') {
                Some(i) => (attr[..i].trim().to_lowercase(), attr[i + 1..].trim().to_string()),
                None => (attr.to_lowercase(), String::new()),
            };
            match k.as_str() {
                "domain" if !v.is_empty() => {
                    domain = v.trim_start_matches('.').to_lowercase();
                    host_only = false;
                }
                "path" if !v.is_empty() => path = v,
                "secure" => secure = true,
                "httponly" => http_only = true,
                "max-age" => {
                    if let Ok(secs) = v.parse::<i64>() {
                        expires = now_unix() + secs;
                    }
                }
                _ => {}
            }
        }

        // __Host-/__Secure- prefix luôn httpOnly+secure thực tế (mirror Python).
        if name.starts_with("__Host-") || name.starts_with("__Secure-") {
            http_only = true;
            secure = true;
        }

        // Xoá cookie cũ cùng (name, domain) rồi push mới (upsert).
        self.items
            .retain(|c| !(c.name == name && c.domain == domain));
        // Set-Cookie với giá trị rỗng + expire quá khứ = xoá cookie.
        if value.is_empty() {
            return;
        }
        self.items.push(CookieEntry {
            name,
            value,
            domain,
            host_only,
            path,
            secure,
            http_only,
            expires,
        });
    }

    /// Build header `Cookie` cho URL (domain + path matching, https-only OK vì
    /// mọi request login đều https).
    fn cookie_header_for(&self, url: &Url) -> String {
        let host = url.host_str().unwrap_or("").to_lowercase();
        let req_path = if url.path().is_empty() { "/" } else { url.path() };
        let mut pairs: Vec<String> = Vec::new();
        for c in &self.items {
            let domain_ok = if c.host_only {
                host == c.domain
            } else {
                host == c.domain || host.ends_with(&format!(".{}", c.domain))
            };
            if !domain_ok {
                continue;
            }
            if !req_path.starts_with(&c.path) {
                continue;
            }
            pairs.push(format!("{}={}", c.name, c.value));
        }
        pairs.join("; ")
    }

    /// Lấy giá trị cookie theo tên (match đầu tiên bất kể domain) — mirror
    /// `curl_cffi session.cookies.get(name)`.
    pub fn get(&self, name: &str) -> Option<String> {
        self.items
            .iter()
            .find(|c| c.name == name)
            .map(|c| c.value.clone())
    }

    /// Có session-token cookie hay chưa (gồm cả chunk `.0`). NextAuth chunk JWT
    /// > 4KB thành `.0/.1/...`; mỗi chunk là cookie riêng — chỉ cần kiểm tra
    /// tồn tại để xác nhận đã đăng nhập.
    pub fn has_session_token(&self) -> bool {
        self.get("__Secure-next-auth.session-token")
            .map(|v| !v.is_empty())
            .unwrap_or(false)
            || self
                .get("__Secure-next-auth.session-token.0")
                .map(|v| !v.is_empty())
                .unwrap_or(false)
    }

    /// Export jar ra schema `__cookies` mà rust bot `build_cookie_header` đọc
    /// được (mirror `session_phase` cookie export).
    pub fn export(&self) -> Vec<Value> {
        self.items
            .iter()
            .map(|c| {
                let domain = if c.host_only {
                    c.domain.clone()
                } else {
                    format!(".{}", c.domain)
                };
                json!({
                    "name": c.name,
                    "value": c.value,
                    "domain": domain,
                    "path": c.path,
                    "secure": c.secure,
                    "httpOnly": c.http_only,
                    "sameSite": "Lax",
                    "expires": c.expires,
                })
            })
            .collect()
    }
}

fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Body cho request login.
pub enum LoginBody<'a> {
    None,
    Json(&'a Value),
    Form(&'a [(String, String)]),
    Text { body: String, content_type: &'a str },
}

/// Kết quả 1 request login (đã đọc body, đã capture set-cookie).
pub struct LoginResponse {
    pub status: u16,
    pub location: Option<String>,
    pub body: String,
    pub final_url: String,
}

impl LoginResponse {
    pub fn is_redirect(&self) -> bool {
        (300..400).contains(&self.status)
    }
    pub fn json(&self) -> Result<Value> {
        serde_json::from_str(&self.body)
            .map_err(|e| anyhow!("JSON parse fail (HTTP {}): {}", self.status, e))
    }
}

pub struct LoginClient {
    http: Client,
    jar: std::sync::Mutex<CookieJar>,
}

impl LoginClient {
    pub fn new(proxy: Option<&str>) -> Result<Self> {
        // Chrome emulation: wreq tự set JA3/JA4 + HTTP2 fingerprint + UA +
        // sec-ch-ua + Accept-Encoding KHỚP NHAU (Chrome 137). KHÔNG override UA
        // ở tầng request (mod.rs) để tránh lệch fingerprint.
        let mut b = Client::builder()
            .emulation(Emulation::Chrome137)
            .timeout(Duration::from_secs(30))
            // Redirect TẮT — ta tự follow từng hop để capture set-cookie.
            .redirect(wreq::redirect::Policy::none());
        // Cookie store: KHÔNG bật feature `cookies` của wreq — jar quản lý thủ
        // công (cần enumerate + reassembly chunk session-token).
        if let Some(p) = proxy {
            // socks5:// → socks5h:// (remote DNS) mirror Python `_create_session`.
            let normalized = if let Some(rest) = p.strip_prefix("socks5://") {
                format!("socks5h://{}", rest)
            } else {
                p.to_string()
            };
            b = b.proxy(Proxy::all(&normalized)?);
        } else {
            b = b.no_proxy();
        }
        Ok(Self {
            http: b.build()?,
            jar: std::sync::Mutex::new(CookieJar::default()),
        })
    }

    pub fn get_cookie(&self, name: &str) -> Option<String> {
        self.jar.lock().unwrap().get(name)
    }

    pub fn has_session_token(&self) -> bool {
        self.jar.lock().unwrap().has_session_token()
    }

    pub fn export_cookies(&self) -> Vec<Value> {
        self.jar.lock().unwrap().export()
    }

    pub fn cookie_count(&self) -> usize {
        self.jar.lock().unwrap().items.len()
    }

    /// 1 request (KHÔNG follow redirect). Tự gắn Cookie + capture Set-Cookie.
    pub async fn send(
        &self,
        method: Method,
        url: &str,
        headers: &[(&str, &str)],
        body: LoginBody<'_>,
    ) -> Result<LoginResponse> {
        let parsed = Url::parse(url).map_err(|e| anyhow!("invalid url {}: {}", url, e))?;
        let cookie_header = self.jar.lock().unwrap().cookie_header_for(&parsed);

        let mut hm = HeaderMap::new();
        for (k, v) in headers {
            let name = HeaderName::from_bytes(k.as_bytes())
                .map_err(|e| anyhow!("bad header name {}: {}", k, e))?;
            let val =
                HeaderValue::from_str(v).map_err(|e| anyhow!("bad header value {}: {}", k, e))?;
            hm.insert(name, val);
        }
        if !cookie_header.is_empty() {
            hm.insert(COOKIE, HeaderValue::from_str(&cookie_header)?);
        }

        let mut req = self.http.request(method, url).headers(hm);
        req = match body {
            LoginBody::None => req,
            LoginBody::Json(v) => req.json(v),
            LoginBody::Form(f) => req.form(f),
            LoginBody::Text { body, content_type } => {
                req.header("content-type", content_type).body(body)
            }
        };

        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let location = resp
            .headers()
            .get(LOCATION)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());

        let host = parsed.host_str().unwrap_or("").to_string();
        {
            let mut jar = self.jar.lock().unwrap();
            for sc in resp.headers().get_all(SET_COOKIE) {
                if let Ok(s) = sc.to_str() {
                    jar.store(s, &host);
                }
            }
        }

        let body_text = resp.text().await.unwrap_or_default();
        Ok(LoginResponse {
            status,
            location,
            body: body_text,
            final_url: url.to_string(),
        })
    }

    /// GET + follow redirect chain (capture cookie mỗi hop). Trả response cuối
    /// (non-redirect) hoặc response redirect cuối khi hết max_hops.
    pub async fn get_follow(
        &self,
        url: &str,
        headers: &[(&str, &str)],
        max_hops: u32,
    ) -> Result<LoginResponse> {
        let mut current = url.to_string();
        for _ in 0..max_hops {
            let mut resp = self.send(Method::GET, &current, headers, LoginBody::None).await?;
            if resp.is_redirect() {
                if let Some(loc) = resp.location.clone() {
                    current = absolutize(&current, &loc)?;
                    continue;
                }
            }
            resp.final_url = current;
            return Ok(resp);
        }
        Err(anyhow!("redirect chain quá dài (> {} hops)", max_hops))
    }
}

/// Resolve relative Location về absolute URL dựa trên URL hiện tại.
pub fn absolutize(base: &str, location: &str) -> Result<String> {
    if location.starts_with("http://") || location.starts_with("https://") {
        return Ok(location.to_string());
    }
    let base_url = Url::parse(base).map_err(|e| anyhow!("bad base url: {}", e))?;
    let joined = base_url
        .join(location)
        .map_err(|e| anyhow!("join location fail: {}", e))?;
    Ok(joined.to_string())
}
