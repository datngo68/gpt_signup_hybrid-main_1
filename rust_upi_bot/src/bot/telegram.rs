//! Telegram bot client — minimal long-poll wrapper trên `reqwest`.
//!
//! Hỗ trợ:
//!   - getUpdates long-poll
//!   - sendMessage (bao gồm reply_to + parse_mode)
//!   - sendDocument (multipart upload PNG QR)
//!   - getFile + download (lấy session.json user upload).

use anyhow::{anyhow, Result};
use reqwest::multipart::{Form, Part};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use crate::bot::send_gate::SendGate;

#[derive(Clone)]
pub struct TelegramClient {
    base_url: String,
    file_base_url: String,
    http: Client,
    /// Bộ điều phối chống 429 — pace per-chat + honor retry_after. Mọi outbound
    /// tới 1 chat đi qua `gate.acquire(chat_id)` trước, và parse `retry_after`
    /// để `gate.penalize` khi dính 429.
    gate: Arc<SendGate>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Update {
    pub update_id: i64,
    pub message: Option<Message>,
    pub callback_query: Option<CallbackQuery>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CallbackQuery {
    pub id: String,
    pub from: User,
    pub message: Option<Message>,
    pub data: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Message {
    pub message_id: i64,
    pub chat: Chat,
    pub from: Option<User>,
    pub text: Option<String>,
    pub document: Option<Document>,
    #[allow(dead_code)]
    pub caption: Option<String>,
    /// Telegram message entities (bold/italic/link/...). Offset/length tính
    /// theo UTF-16 code unit. Giữ raw `Value` để re-send nguyên format.
    pub entities: Option<Vec<Value>>,
    #[allow(dead_code)]
    pub date: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Chat {
    pub id: i64,
    #[serde(rename = "type")]
    #[allow(dead_code)]
    pub chat_type: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct User {
    pub id: i64,
    pub username: Option<String>,
    pub first_name: Option<String>,
    #[allow(dead_code)]
    pub is_bot: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Document {
    pub file_id: String,
    pub file_name: Option<String>,
    pub file_size: Option<u64>,
    #[allow(dead_code)]
    pub mime_type: Option<String>,
}

impl TelegramClient {
    pub fn new(token: &str) -> Result<Self> {
        let http = Client::builder()
            .timeout(Duration::from_secs(120))
            .build()?;
        Ok(Self {
            base_url: format!("https://api.telegram.org/bot{}", token),
            file_base_url: format!("https://api.telegram.org/file/bot{}", token),
            http,
            gate: Arc::new(SendGate::new()),
        })
    }

    /// Bộ điều phối gửi (dùng cho vacuum loop dọn map).
    pub fn gate(&self) -> &Arc<SendGate> {
        &self.gate
    }

    /// Parse `retry_after` từ response 429 → đẩy mốc gửi tiếp của chat. Gọi sau
    /// MỌI response gửi-tới-chat khi `ok != true`. Telegram đặt `retry_after`
    /// trong `parameters` (đôi khi cũng nhúng trong description "retry after N").
    async fn note_429(&self, chat_id: i64, v: &Value) {
        if v.get("ok").and_then(|b| b.as_bool()) == Some(true) {
            return;
        }
        let ra = v
            .get("parameters")
            .and_then(|p| p.get("retry_after"))
            .and_then(|x| x.as_u64())
            .or_else(|| {
                v.get("description")
                    .and_then(|s| s.as_str())
                    .filter(|s| s.contains("retry after"))
                    .and_then(|s| s.rsplit("retry after").next())
                    .and_then(|tail| tail.trim().split_whitespace().next())
                    .and_then(|n| n.parse::<u64>().ok())
            });
        if let Some(secs) = ra {
            self.gate.penalize(chat_id, secs).await;
        }
    }

    pub async fn get_updates(&self, offset: i64, timeout: u64) -> Result<Vec<Update>> {
        let url = format!("{}/getUpdates", self.base_url);
        let resp = self
            .http
            .get(&url)
            .query(&[
                ("offset", offset.to_string()),
                ("timeout", timeout.to_string()),
                ("allowed_updates", r#"["message","callback_query"]"#.into()),
            ])
            .send()
            .await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "getUpdates fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        let updates: Vec<Update> = serde_json::from_value(
            v.get("result").cloned().unwrap_or(Value::Array(vec![])),
        )?;
        Ok(updates)
    }

    /// Đăng ký bot commands cho menu Telegram (`/`).
    /// `commands` — slice (command_name_no_slash, description).
    pub async fn set_my_commands(&self, commands: &[(&str, &str)]) -> Result<()> {
        let url = format!("{}/setMyCommands", self.base_url);
        let cmds: Vec<Value> = commands
            .iter()
            .map(|(c, d)| json!({"command": *c, "description": *d}))
            .collect();
        let body = json!({ "commands": cmds });
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "setMyCommands fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(())
    }

    /// Đăng ký bot commands cho riêng 1 chat (scope=chat) — dùng để hiện
    /// nhóm lệnh admin (`/notify`, `/ban`, ...) chỉ trong chat của admin,
    /// không lộ ra menu của user thường.
    pub async fn set_my_commands_for_chat(
        &self,
        chat_id: i64,
        commands: &[(&str, &str)],
    ) -> Result<()> {
        let url = format!("{}/setMyCommands", self.base_url);
        let cmds: Vec<Value> = commands
            .iter()
            .map(|(c, d)| json!({"command": *c, "description": *d}))
            .collect();
        let body = json!({
            "commands": cmds,
            "scope": { "type": "chat", "chat_id": chat_id },
        });
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "setMyCommands(chat) fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(())
    }

    /// Trả lời callback query (đóng spinner trên client). `text` optional —
    /// nếu set sẽ hiện toast nhỏ.
    pub async fn answer_callback_query(
        &self,
        callback_id: &str,
        text: Option<&str>,
    ) -> Result<()> {
        let url = format!("{}/answerCallbackQuery", self.base_url);
        let mut body = json!({ "callback_query_id": callback_id });
        if let Some(t) = text {
            body["text"] = json!(t);
        }
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            tracing::debug!(
                "answerCallbackQuery warn: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            );
        }
        Ok(())
    }

    pub async fn send_message(
        &self,
        chat_id: i64,
        text: &str,
        reply_to: Option<i64>,
    ) -> Result<i64> {
        self.send_message_inner(chat_id, text, reply_to, None).await
    }

    /// Gửi message tới group/supergroup, có hỗ trợ topic (forum thread_id).
    /// Khi `thread_id = Some(n)` Telegram sẽ gửi vào đúng topic đó. Dùng cho
    /// kênh thông báo `notify.chat_id` admin tự cấu hình.
    pub async fn send_message_to_thread(
        &self,
        chat_id: i64,
        text: &str,
        thread_id: Option<i64>,
    ) -> Result<i64> {
        let url = format!("{}/sendMessage", self.base_url);
        let mut body = json!({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": true,
        });
        if let Some(t) = thread_id {
            if t > 0 {
                body["message_thread_id"] = json!(t);
            }
        }
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendMessage(thread) fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(v["result"]["message_id"].as_i64().unwrap_or(0))
    }

    /// Gửi message kèm inline keyboard. `keyboard` là JSON array of array of
    /// button objects (chuẩn Telegram InlineKeyboardMarkup).
    pub async fn send_message_kb(
        &self,
        chat_id: i64,
        text: &str,
        reply_to: Option<i64>,
        keyboard: Value,
    ) -> Result<i64> {
        self.send_message_inner(chat_id, text, reply_to, Some(keyboard))
            .await
    }

    /// Gửi message HTML (parse_mode=HTML) + inline keyboard. Caller PHẢI escape
    /// các giá trị dynamic (email, username, ...) bằng `html_escape` trước khi
    /// nhúng vào html — Telegram parse rất chặt, ký tự `<`/`>`/`&` không escape
    /// sẽ làm fail toàn bộ message.
    pub async fn send_message_kb_html(
        &self,
        chat_id: i64,
        html: &str,
        keyboard: Value,
    ) -> Result<i64> {
        let url = format!("{}/sendMessage", self.base_url);
        let body = json!({
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": true,
            "reply_markup": { "inline_keyboard": keyboard },
        });
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendMessage(html) fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(v["result"]["message_id"].as_i64().unwrap_or(0))
    }

    /// Edit message HTML (parse_mode=HTML) + giữ inline keyboard. Dùng cho
    /// dashboard per-user (cập nhật bảng tiến trình). `message is not modified`
    /// coi như OK (no-op).
    pub async fn edit_message_kb_html(
        &self,
        chat_id: i64,
        message_id: i64,
        html: &str,
        keyboard: Value,
    ) -> Result<()> {
        let url = format!("{}/editMessageText", self.base_url);
        let body = json!({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": true,
            "reply_markup": { "inline_keyboard": keyboard },
        });
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            let desc = v.get("description").and_then(|s| s.as_str()).unwrap_or("?");
            if desc.contains("message is not modified") {
                return Ok(());
            }
            return Err(anyhow!("editMessageText(html) fail: {}", desc));
        }
        Ok(())
    }

    async fn send_message_inner(
        &self,
        chat_id: i64,
        text: &str,
        reply_to: Option<i64>,
        reply_markup: Option<Value>,
    ) -> Result<i64> {
        let url = format!("{}/sendMessage", self.base_url);
        let mut body = json!({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": true,
        });
        if let Some(r) = reply_to {
            body["reply_to_message_id"] = json!(r);
        }
        if let Some(kb) = reply_markup {
            body["reply_markup"] = json!({ "inline_keyboard": kb });
        }
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendMessage fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(v["result"]["message_id"].as_i64().unwrap_or(0))
    }

    /// Gửi message kèm `entities` thô (giữ nguyên format admin đã gõ:
    /// bold/italic/link/xuống dòng...). Khác `send_message_kb` ở chỗ KHÔNG
    /// dùng parse_mode — Telegram render trực tiếp theo entities.
    pub async fn send_message_entities(
        &self,
        chat_id: i64,
        text: &str,
        entities: Option<Vec<Value>>,
    ) -> Result<i64> {
        let url = format!("{}/sendMessage", self.base_url);
        let mut body = json!({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": true,
        });
        if let Some(ents) = entities {
            if !ents.is_empty() {
                body["entities"] = Value::Array(ents);
            }
        }
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendMessage fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(v["result"]["message_id"].as_i64().unwrap_or(0))
    }

    pub async fn edit_message_text(
        &self,
        chat_id: i64,
        message_id: i64,
        text: &str,
    ) -> Result<()> {
        self.edit_message_text_inner(chat_id, message_id, text, None).await
    }

    /// Gửi `sendRichMessage` (Bot API 10.1+) — message giàu định dạng với
    /// `<table>`, `<h1>..<h6>`, `<details>`, `<hr>`, list... Caller PHẢI
    /// escape mọi giá trị động trước khi nhúng vào `rich_html`.
    ///
    /// Telegram render trên client mới (Desktop 6.9+ / mobile mới); client cũ
    /// hiển thị rút gọn nhưng API vẫn nhận. Khi API trả lỗi (vd Bot API server
    /// chưa update lên 10.1), trả `Err` để caller log/handle — KHÔNG fallback
    /// ngầm để tránh che lỗi.
    pub async fn send_rich_message_kb(
        &self,
        chat_id: i64,
        rich_html: &str,
        keyboard: Value,
    ) -> Result<i64> {
        let url = format!("{}/sendRichMessage", self.base_url);
        let body = json!({
            "chat_id": chat_id,
            "rich_message": { "html": rich_html },
            "reply_markup": { "inline_keyboard": keyboard },
        });
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendRichMessage fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(v["result"]["message_id"].as_i64().unwrap_or(0))
    }

    /// Edit `rich_message` (Bot API 10.1+) + giữ inline keyboard.
    /// `message is not modified` được coi là OK (no-op).
    pub async fn edit_rich_message_kb(
        &self,
        chat_id: i64,
        message_id: i64,
        rich_html: &str,
        keyboard: Value,
    ) -> Result<()> {
        let url = format!("{}/editMessageText", self.base_url);
        let body = json!({
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": { "html": rich_html },
            "reply_markup": { "inline_keyboard": keyboard },
        });
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            let desc = v.get("description").and_then(|s| s.as_str()).unwrap_or("?");
            if desc.contains("message is not modified") {
                return Ok(());
            }
            tracing::debug!("editMessageText(rich) warn: {}", desc);
        }
        Ok(())
    }

    async fn edit_message_text_inner(
        &self,
        chat_id: i64,
        message_id: i64,
        text: &str,
        reply_markup: Option<Value>,
    ) -> Result<()> {
        let url = format!("{}/editMessageText", self.base_url);
        let mut body = json!({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": true,
        });
        if let Some(kb) = reply_markup {
            body["reply_markup"] = json!({ "inline_keyboard": kb });
        }
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            tracing::debug!(
                "editMessageText warn: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            );
        }
        Ok(())
    }

    pub async fn send_photo(
        &self,
        chat_id: i64,
        photo_path: &Path,
        caption: Option<&str>,
        reply_to: Option<i64>,
    ) -> Result<()> {
        let url = format!("{}/sendPhoto", self.base_url);
        let bytes = std::fs::read(photo_path)?;
        let part = Part::bytes(bytes)
            .file_name(
                photo_path
                    .file_name()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_else(|| "qr.png".into()),
            )
            .mime_str("image/png")?;
        let mut form = Form::new()
            .text("chat_id", chat_id.to_string())
            .part("photo", part);
        if let Some(c) = caption {
            form = form.text("caption", c.to_string());
        }
        if let Some(r) = reply_to {
            form = form.text("reply_to_message_id", r.to_string());
        }
        self.gate.acquire(chat_id).await;
        let resp = self.http.post(&url).multipart(form).send().await?;
        let v: Value = resp.json().await?;
        self.note_429(chat_id, &v).await;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendPhoto fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(())
    }

    pub async fn get_file_path(&self, file_id: &str) -> Result<String> {
        let url = format!("{}/getFile", self.base_url);
        let resp = self
            .http
            .get(&url)
            .query(&[("file_id", file_id)])
            .send()
            .await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "getFile fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        let path = v["result"]["file_path"]
            .as_str()
            .ok_or_else(|| anyhow!("getFile missing file_path"))?
            .to_string();
        Ok(path)
    }

    pub async fn download_file(&self, file_path: &str) -> Result<bytes::Bytes> {
        let url = format!("{}/{}", self.file_base_url, file_path);
        let resp = self.http.get(&url).send().await?;
        Ok(resp.bytes().await?)
    }
}
