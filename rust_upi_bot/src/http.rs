//! HTTP client wrapper trên `reqwest` — hỗ trợ proxy per-request,
//! retry/timeout, headers preset Chrome.
//!
//! Chiến lược: dùng 1 connection pool chính cho DIRECT, build per-request
//! client khi cần proxy (tradeoff: tạo client mỗi request có overhead nhưng
//! đơn giản và đúng — reqwest không hỗ trợ proxy override per request).

use anyhow::{anyhow, Result};
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use reqwest::{Client, ClientBuilder, Method, Proxy};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct HttpResponse {
    pub status: u16,
    #[allow(dead_code)]
    pub headers: HeaderMap,
    pub body: String,
}

#[derive(Debug, Clone)]
pub struct HttpResponseBytes {
    pub status: u16,
    pub headers: HeaderMap,
    pub body: bytes::Bytes,
}

pub struct HttpClient {
    direct: Client,
    /// Cache proxy clients keyed by proxy URL string.
    proxy_clients: tokio::sync::RwLock<HashMap<String, Client>>,
    timeout: Duration,
}

impl HttpClient {
    pub fn new(timeout_seconds: u64) -> Result<Arc<Self>> {
        let timeout = Duration::from_secs(timeout_seconds);
        let direct = build_client(None, timeout)?;
        Ok(Arc::new(Self {
            direct,
            proxy_clients: tokio::sync::RwLock::new(HashMap::new()),
            timeout,
        }))
    }

    /// Lấy reqwest::Client cho proxy_url cho trước (None = direct).
    async fn client_for(&self, proxy_url: Option<&str>) -> Result<Client> {
        let Some(proxy) = proxy_url else {
            return Ok(self.direct.clone());
        };
        {
            let read = self.proxy_clients.read().await;
            if let Some(c) = read.get(proxy) {
                return Ok(c.clone());
            }
        }
        let mut write = self.proxy_clients.write().await;
        if let Some(c) = write.get(proxy) {
            return Ok(c.clone());
        }
        let client = build_client(Some(proxy), self.timeout)?;
        write.insert(proxy.to_string(), client.clone());
        Ok(client)
    }

    pub async fn get_text(
        &self,
        url: &str,
        headers: &HashMap<&str, &str>,
        proxy: Option<&str>,
    ) -> Result<HttpResponse> {
        self.request_text(Method::GET, url, headers, None, proxy).await
    }

    pub async fn post_form(
        &self,
        url: &str,
        headers: &HashMap<&str, &str>,
        form: &[(String, String)],
        proxy: Option<&str>,
    ) -> Result<HttpResponse> {
        let client = self.client_for(proxy).await?;
        let mut req = client.request(Method::POST, url);
        let hm = build_header_map(headers)?;
        req = req.headers(hm);
        req = req.form(form);
        let resp = req.send().await?;
        decode_text(resp).await
    }

    pub async fn post_json(
        &self,
        url: &str,
        headers: &HashMap<&str, &str>,
        body: &serde_json::Value,
        proxy: Option<&str>,
    ) -> Result<HttpResponse> {
        let client = self.client_for(proxy).await?;
        let mut req = client.request(Method::POST, url);
        let hm = build_header_map(headers)?;
        req = req.headers(hm);
        req = req.json(body);
        let resp = req.send().await?;
        decode_text(resp).await
    }

    pub async fn get_with_query(
        &self,
        url: &str,
        headers: &HashMap<&str, &str>,
        query: &[(String, String)],
        proxy: Option<&str>,
    ) -> Result<HttpResponse> {
        let client = self.client_for(proxy).await?;
        let mut req = client.request(Method::GET, url);
        let hm = build_header_map(headers)?;
        req = req.headers(hm);
        req = req.query(query);
        let resp = req.send().await?;
        decode_text(resp).await
    }

    pub async fn get_bytes(
        &self,
        url: &str,
        headers: &HashMap<&str, &str>,
        proxy: Option<&str>,
    ) -> Result<HttpResponseBytes> {
        let client = self.client_for(proxy).await?;
        let mut req = client.request(Method::GET, url);
        let hm = build_header_map(headers)?;
        req = req.headers(hm);
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let headers = resp.headers().clone();
        let body = resp.bytes().await?;
        Ok(HttpResponseBytes { status, headers, body })
    }

    pub async fn head(&self, url: &str, timeout_secs: u64) -> Result<u16> {
        let mut req = self.direct.request(Method::HEAD, url);
        req = req.timeout(Duration::from_secs(timeout_secs));
        let resp = req.send().await?;
        Ok(resp.status().as_u16())
    }

    async fn request_text(
        &self,
        method: Method,
        url: &str,
        headers: &HashMap<&str, &str>,
        body: Option<bytes::Bytes>,
        proxy: Option<&str>,
    ) -> Result<HttpResponse> {
        let client = self.client_for(proxy).await?;
        let mut req = client.request(method, url);
        let hm = build_header_map(headers)?;
        req = req.headers(hm);
        if let Some(b) = body {
            req = req.body(b);
        }
        let resp = req.send().await?;
        decode_text(resp).await
    }
}

fn build_client(proxy: Option<&str>, timeout: Duration) -> Result<Client> {
    let mut b = ClientBuilder::new()
        .timeout(timeout)
        // Mỗi request mở TCP/TLS mới — tránh stale connection bị Cloudflare
        // hoặc origin server đóng âm thầm gây stall 30s timeout. Trade-off:
        // chậm hơn ~80ms/request do TLS handshake, nhưng reliable hơn nhiều
        // trên router với upstream Cloudflare.
        .pool_max_idle_per_host(0)
        .pool_idle_timeout(Some(Duration::from_secs(0)))
        .tcp_keepalive(Duration::from_secs(15))
        // Force HTTP/1.1 — loại bỏ HTTP/2 stream multiplexing stall vốn xảy ra
        // khi 1 stream bị server reset mà client không phát hiện.
        .http1_only()
        .cookie_store(true);
    if let Some(p) = proxy {
        b = b.proxy(Proxy::all(p)?);
    }
    Ok(b.build()?)
}

fn build_header_map(map: &HashMap<&str, &str>) -> Result<HeaderMap> {
    let mut hm = HeaderMap::new();
    for (k, v) in map {
        let name = HeaderName::from_bytes(k.as_bytes())
            .map_err(|e| anyhow!("invalid header name {}: {}", k, e))?;
        let val = HeaderValue::from_str(v)
            .map_err(|e| anyhow!("invalid header value for {}: {}", k, e))?;
        hm.insert(name, val);
    }
    Ok(hm)
}

async fn decode_text(resp: reqwest::Response) -> Result<HttpResponse> {
    let status = resp.status().as_u16();
    let headers = resp.headers().clone();
    let body = resp.text().await.unwrap_or_default();
    Ok(HttpResponse { status, headers, body })
}
