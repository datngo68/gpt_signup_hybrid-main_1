//! Lớp hiển thị thân thiện cho user Telegram:
//!   - `render_dashboard_html`: bảng tiến trình per-user (1 message sống).
//!   - `qr_caption_full`: caption ảnh QR khi thành công (gộp link + hạn).
//!   - `render_done_fail` / `render_timeout`: tin terminal lý do đời thường.
//!   - `render_preflight_ok` / `_batch`: card kiểm tra proxy trước khi chạy.
//!   - `render_proxy_line`: 1 dòng trạng thái proxy cho card pre-flight.
//!
//! Log thô của runner vẫn được parse thành `StepKind` ở `board.rs` (dùng cho
//! dashboard) — đây chỉ là các template text.

use crate::bot::i18n::Lang;

/// 1 dòng trạng thái proxy cho pre-flight card. `label` = "Login"/"User #i".
pub fn render_proxy_line(
    lang: Lang,
    label: &str,
    ok: bool,
    status: &str,
    detail: &str,
    latency_ms: u64,
) -> String {
    let icon = if ok { "✅" } else { "❌" };
    if ok {
        let ip = match lang {
            Lang::Vi => "IP",
            Lang::En => "IP",
        };
        format!("{} {}: {} · {} {} · {}ms", icon, label, status, ip, detail, latency_ms)
    } else {
        format!("{} {}: {} · {}", icon, label, status, detail)
    }
}

/// Card pre-flight (1 job) khi proxy OK → thông báo ngắn rồi vào hàng chờ.
pub fn render_preflight_ok(lang: Lang, email: &str, lines: &[String]) -> String {
    let body = lines.join("\n");
    match lang {
        Lang::Vi => format!("🌐 Kiểm tra proxy · {}\n{}", email, body),
        Lang::En => format!("🌐 Proxy check · {}\n{}", email, body),
    }
}

/// Card pre-flight cho BATCH nhiều combo — gộp 1 lần probe + 1 card cho cả
/// batch (thay vì N card trùng lặp khi user dán nhiều dòng).
pub fn render_preflight_ok_batch(lang: Lang, count: usize, lines: &[String]) -> String {
    let body = lines.join("\n");
    match lang {
        Lang::Vi => format!("🌐 Kiểm tra proxy · {} tài khoản\n{}", count, body),
        Lang::En => format!("🌐 Proxy check · {} accounts\n{}", count, body),
    }
}

/// Dashboard per-user (1 message sống) — gom MỌI tiến trình của user vào 1
/// message HTML (parse_mode=HTML cổ điển, KHÔNG dùng `<table>` của rich để
/// chạy được trên mọi Bot API server). Mỗi tiến trình 2 dòng: email + tuổi,
/// rồi bước thân thiện. Cập nhật qua editMessageText có throttle → chỉ 1
/// message/user thay vì N → chống flood 429.
///
/// `entries` lấy từ `JobBoard::snapshot_entries_for_user`. Mọi giá trị động
/// đã đi qua `html_escape`.
pub fn render_dashboard_html(
    entries: &[(u64, crate::bot::board::JobStatus)],
    lang: Lang,
    clock: &str,
) -> String {
    use crate::bot::board::{fmt_age, html_escape, JobState};

    if entries.is_empty() {
        return match lang {
            Lang::Vi => "✅ <b>Không còn tiến trình nào đang chạy.</b>\n<i>Gửi tài khoản (file session.json hoặc combo) để bắt đầu.</i>".to_string(),
            Lang::En => "✅ <b>No active processes.</b>\n<i>Send an account (session.json file or combo) to start.</i>".to_string(),
        };
    }

    let running = entries
        .iter()
        .filter(|(_, s)| s.state == JobState::Running)
        .count();
    let queued = entries.len() - running;

    let (title, run_w, queue_w, foot) = match lang {
        Lang::Vi => (
            "📊 <b>Tiến trình của bạn</b>",
            "chạy",
            "chờ",
            "<i>Bảng tự cập nhật. Bấm 🛑 để dừng tiến trình tương ứng.</i>",
        ),
        Lang::En => (
            "📊 <b>Your processes</b>",
            "run",
            "queue",
            "<i>Auto-updating. Tap 🛑 to stop a process.</i>",
        ),
    };

    let mut out = String::with_capacity(128 + entries.len() * 96);
    out.push_str(title);
    out.push('\n');
    out.push_str(&format!(
        "<i>▶️ <b>{}</b> {} · ⏳ <b>{}</b> {} · 🕒 {}</i>\n",
        running,
        run_w,
        queued,
        queue_w,
        html_escape(clock)
    ));
    out.push_str("───────────────\n");

    for (i, (_, s)) in entries.iter().enumerate() {
        let icon = match s.state {
            JobState::Running => "▶️",
            JobState::Queued => "⏳",
        };
        let age = fmt_age(s.since.elapsed().as_secs());
        let step = html_escape(&s.step.label(lang));
        out.push_str(&format!(
            "{} <b>{}.</b> <code>{}</code> · 🕒 <b>{}</b>\n     <i>↳ {}</i>\n",
            icon,
            i + 1,
            html_escape(&s.email_masked),
            html_escape(&age),
            step,
        ));
    }
    out.push('\n');
    out.push_str(foot);
    out
}

/// Caption ảnh QR khi thành công — gộp email + hạn + (tùy chọn) link thanh
/// toán + số lần kích hoạt vào 1 caption (không gửi tin riêng → bớt 1 message).
pub fn qr_caption_full(
    lang: Lang,
    email: &str,
    expires: &str,
    payment_link: Option<&str>,
    attempts: usize,
) -> String {
    let activated = if attempts > 0 {
        match lang {
            Lang::Vi => format!(" · kích hoạt sau {} lần", attempts),
            Lang::En => format!(" · activated after {} tries", attempts),
        }
    } else {
        String::new()
    };
    let mut s = match lang {
        Lang::Vi => format!("✅ UPI QR · {}\nHết hạn: {}{}", email, expires, activated),
        Lang::En => format!("✅ UPI QR · {}\nExpires: {}{}", email, expires, activated),
    };
    if let Some(url) = payment_link {
        s.push_str(&match lang {
            Lang::Vi => format!("\n💳 Link thanh toán: {}", url),
            Lang::En => format!("\n💳 Payment link: {}", url),
        });
    }
    s
}

/// Tin terminal khi job FAIL — dịch lỗi thô sang câu đời thường.
pub fn render_done_fail(
    lang: Lang,
    email: &str,
    elapsed_s: f64,
    raw_error: &str,
    attempts: usize,
) -> String {
    let reason = friendly_reason(lang, raw_error);
    let tries = if attempts > 0 {
        match lang {
            Lang::Vi => format!(" · đã thử {} lần", attempts),
            Lang::En => format!(" · {} tries", attempts),
        }
    } else {
        String::new()
    };
    match lang {
        Lang::Vi => format!(
            "❌ KHÔNG THÀNH CÔNG · {}\n⏱ {}{}\n\nLý do: {}\n↳ Gửi lại để thử tài khoản khác.",
            email,
            fmt_elapsed(elapsed_s),
            tries,
            reason
        ),
        Lang::En => format!(
            "❌ FAILED · {}\n⏱ {}{}\n\nReason: {}\n↳ Send again to try another account.",
            email,
            fmt_elapsed(elapsed_s),
            tries,
            reason
        ),
    }
}

/// Tin terminal khi job hết thời gian.
pub fn render_timeout(lang: Lang, email: &str, elapsed_s: f64) -> String {
    match lang {
        Lang::Vi => format!(
            "⏰ HẾT THỜI GIAN · {}\n⏱ {}\n\nĐã dừng để giải phóng tài nguyên. Bạn có thể gửi lại.",
            email,
            fmt_elapsed(elapsed_s)
        ),
        Lang::En => format!(
            "⏰ TIMED OUT · {}\n⏱ {}\n\nStopped to free resources. You can retry.",
            email,
            fmt_elapsed(elapsed_s)
        ),
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────

/// Dịch lỗi thô của runner sang câu đời thường.
fn friendly_reason(lang: Lang, raw: &str) -> String {
    let r = raw.to_lowercase();
    let vi = matches!(lang, Lang::Vi);
    let pick = |v: &str, e: &str| -> String { if vi { v.into() } else { e.into() } };

    if r.contains("passwordless otp") || r.contains("email-verification") || r.contains("mailbox") {
        return pick(
            "Tài khoản đăng nhập bằng mã gửi vào email (cần hộp thư) — hãy gửi session.json thay vì combo.",
            "This account signs in via an email code (needs a mailbox) — send session.json instead of a combo.",
        );
    }
    if r.contains("login fail") || r.contains("mfa") || r.contains("password verify") {
        return pick(
            "Đăng nhập thất bại (sai mật khẩu/2FA, hoặc bị chặn).",
            "Sign-in failed (wrong password/2FA, or blocked).",
        );
    }
    if r.contains("billing country") {
        return pick(
            "Cần proxy đúng quốc gia (Ấn Độ) cho tài khoản này.",
            "This account needs a matching-country (India) proxy.",
        );
    }
    if r.contains("no free offer") || r.contains("promo") || r.contains("amount") {
        return pick(
            "Tài khoản chưa đủ điều kiện nhận ưu đãi UPI miễn phí.",
            "This account isn't eligible for the free UPI offer.",
        );
    }
    if r.contains("blocked") || r.contains("approve failed") || r.contains("not approved") {
        return pick(
            "Chưa đủ điều kiện nhận ưu đãi UPI (máy chủ từ chối).",
            "Not eligible for the UPI offer (server rejected).",
        );
    }
    if r.contains("no upi") || r.contains("qr image") || r.contains("qr ") {
        return pick(
            "Không lấy được mã QR từ máy chủ.",
            "Couldn't obtain the QR code from the server.",
        );
    }
    if r.contains("network") || r.contains("outage") || r.contains("timeout") || r.contains("checkout http") {
        return pick(
            "Mạng/máy chủ không ổn định, vui lòng thử lại.",
            "Network/server unstable, please try again.",
        );
    }
    let short: String = raw.chars().take(120).collect();
    if vi {
        format!("Không hoàn tất ({}).", short)
    } else {
        format!("Could not complete ({}).", short)
    }
}

fn fmt_elapsed(secs: f64) -> String {
    let s = secs.max(0.0) as u64;
    if s < 60 {
        format!("{}s", s)
    } else {
        format!("{}m{:02}s", s / 60, s % 60)
    }
}
