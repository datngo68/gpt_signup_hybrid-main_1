//! QR rendering — render UPI URI thành PNG với watermark text bên dưới
//! (không đè lên QR pixels → 0% rủi ro hỏng QR), hoặc download trực tiếp
//! từ Stripe image URL rồi composite watermark.

use crate::http::HttpClient;
use crate::upi::matchers::extract_hosted_upi_uri;
use anyhow::{anyhow, Result};
use image::{GenericImage, ImageBuffer, Luma};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct QrDownloadResult {
    #[allow(dead_code)]
    pub downloaded: bool,
    pub rendered: bool,
    pub path: Option<PathBuf>,
    pub source: Option<String>,
    #[allow(dead_code)]
    pub html_path: Option<PathBuf>,
    pub reason: Option<String>,
    #[allow(dead_code)]
    pub bytes: Option<u64>,
}

/// Render UPI URI thành PNG file at `out_path` với watermark dưới.
pub fn render_qr_png(uri: &str, out_path: &Path, watermark: Option<&str>) -> Result<()> {
    if let Some(parent) = out_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let code = qrcode::QrCode::new(uri.as_bytes())
        .map_err(|e| anyhow!("qrcode build fail: {}", e))?;
    let qr_image = code
        .render::<Luma<u8>>()
        .min_dimensions(300, 300)
        .max_dimensions(600, 600)
        .quiet_zone(true)
        .build();
    let final_image = match watermark {
        Some(text) if !text.is_empty() => add_watermark_below(qr_image, text),
        _ => qr_image,
    };
    final_image.save(out_path)?;
    Ok(())
}

/// Composite watermark vào PNG đã có sẵn (case download từ Stripe).
/// Nếu lỗi decode (vd file là SVG) → giữ nguyên file gốc, log warn.
pub fn apply_watermark_to_png(path: &Path, watermark: &str) -> Result<()> {
    if watermark.is_empty() {
        return Ok(());
    }
    let img = image::open(path).map_err(|e| anyhow!("open png fail: {}", e))?;
    let luma = img.to_luma8();
    let result = add_watermark_below(luma, watermark);
    result.save(path)?;
    Ok(())
}

/// Tạo canvas mới = QR (đã crop bottom quiet zone) + dải watermark sát ngay
/// dưới modules. Cách này cho text gần QR thật sự, không bị quiet zone trắng
/// đẩy ra xa.
fn add_watermark_below(
    qr: ImageBuffer<Luma<u8>, Vec<u8>>,
    text: &str,
) -> ImageBuffer<Luma<u8>, Vec<u8>> {
    let (qr_w, qr_h_orig) = qr.dimensions();

    // Crop bottom quiet zone — tìm hàng cuối có pixel đen (end of QR modules).
    let bottom_dark = find_bottom_dark_row(&qr);
    let qr_h = (bottom_dark + 1).min(qr_h_orig);

    // Cấu hình hiển thị
    let font_scale: u32 = 3;
    let char_w: u32 = 8 * font_scale;
    let char_h: u32 = 8 * font_scale;
    let pad_top: u32 = 6; // khoảng nhỏ giữa modules và text — sát QR
    let pad_bottom: u32 = 10; // chừa khoảng trắng đáy đẹp
    let band_h: u32 = char_h + pad_top + pad_bottom;

    let canvas_w = qr_w;
    let canvas_h = qr_h + band_h;

    let mut canvas: ImageBuffer<Luma<u8>, Vec<u8>> =
        ImageBuffer::from_pixel(canvas_w, canvas_h, Luma([255u8]));

    // Chỉ copy phần QR đã crop (không bao gồm bottom quiet zone)
    let cropped = image::imageops::crop_imm(&qr, 0, 0, qr_w, qr_h).to_image();
    canvas.copy_from(&cropped, 0, 0).ok();

    let text_chars = text.chars().count() as u32;
    let text_w = text_chars * char_w;
    let x_start = if canvas_w > text_w {
        (canvas_w - text_w) / 2
    } else {
        0
    };
    let y_start = qr_h + pad_top;

    draw_text_8x8(&mut canvas, text, x_start, y_start, font_scale);
    canvas
}

/// Tìm hàng cuối cùng (y lớn nhất) có chứa pixel đen — biên dưới của QR modules.
/// Trả về `h-1` nếu không có pixel đen (ảnh trắng toàn bộ).
fn find_bottom_dark_row(img: &ImageBuffer<Luma<u8>, Vec<u8>>) -> u32 {
    let (w, h) = img.dimensions();
    for y in (0..h).rev() {
        for x in 0..w {
            if img.get_pixel(x, y).0[0] < 128 {
                return y;
            }
        }
    }
    h.saturating_sub(1)
}

/// Vẽ text ASCII (bitmap 8x8) lên canvas. Text được scale lên `scale` lần.
/// Pixel "on" → đen (Luma 0), "off" → bỏ qua (giữ nguyên background).
fn draw_text_8x8(
    canvas: &mut ImageBuffer<Luma<u8>, Vec<u8>>,
    text: &str,
    x_start: u32,
    y_start: u32,
    scale: u32,
) {
    use font8x8::UnicodeFonts;
    let mut x_offset = 0u32;
    for ch in text.chars() {
        if let Some(glyph) = font8x8::BASIC_FONTS.get(ch) {
            let glyph: [u8; 8] = glyph;
            for (row, byte) in glyph.iter().enumerate() {
                for col in 0..8u32 {
                    if (byte >> col) & 1 == 1 {
                        for sy in 0..scale {
                            for sx in 0..scale {
                                let px = x_start + x_offset + col * scale + sx;
                                let py = y_start + (row as u32) * scale + sy;
                                if px < canvas.width() && py < canvas.height() {
                                    canvas.put_pixel(px, py, Luma([0u8]));
                                }
                            }
                        }
                    }
                }
            }
        }
        x_offset += 8 * scale;
    }
}

pub async fn download_qr_image(
    client: &HttpClient,
    url: &str,
    out_path: &Path,
    proxy: Option<&str>,
    watermark: Option<&str>,
) -> Result<QrDownloadResult> {
    if let Some(parent) = out_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let resp = client
        .get_bytes(url, &HashMap::new(), proxy)
        .await
        .map_err(|e| anyhow!("download fail: {}", e))?;
    if resp.status != 200 {
        return Ok(QrDownloadResult {
            downloaded: false,
            rendered: false,
            path: None,
            source: None,
            html_path: None,
            reason: Some(format!("status {}", resp.status)),
            bytes: None,
        });
    }
    let content_type = resp
        .headers
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_lowercase();
    let body = resp.body;
    let looks_like_html = content_type.contains("text/html")
        || body
            .iter()
            .take(64)
            .map(|b| b.to_ascii_lowercase())
            .collect::<Vec<u8>>()
            .windows(5)
            .any(|w| w == b"<html");

    if looks_like_html {
        let html_path = out_path.with_extension("html");
        std::fs::write(&html_path, &body)?;
        let html_text = String::from_utf8_lossy(&body);
        match extract_hosted_upi_uri(&html_text) {
            Some(uri) => {
                render_qr_png(&uri, out_path, watermark)?;
                let size = std::fs::metadata(out_path).map(|m| m.len()).ok();
                Ok(QrDownloadResult {
                    downloaded: false,
                    rendered: true,
                    path: Some(out_path.to_path_buf()),
                    source: Some("hosted_instructions_html".into()),
                    html_path: Some(html_path),
                    reason: None,
                    bytes: size,
                })
            }
            None => Ok(QrDownloadResult {
                downloaded: false,
                rendered: false,
                path: None,
                source: None,
                html_path: Some(html_path),
                reason: Some("hosted instructions HTML did not contain mobile_auth_url".into()),
                bytes: None,
            }),
        }
    } else {
        std::fs::write(out_path, &body)?;
        // Watermark chỉ áp cho PNG raster (skip SVG để tránh decode lỗi).
        let is_png = matches!(out_path.extension().and_then(|e| e.to_str()), Some("png"));
        if is_png {
            if let Some(text) = watermark.filter(|t| !t.is_empty()) {
                if let Err(e) = apply_watermark_to_png(out_path, text) {
                    tracing::warn!("apply watermark fail: {}", e);
                }
            }
        }
        Ok(QrDownloadResult {
            downloaded: true,
            rendered: true,
            path: Some(out_path.to_path_buf()),
            source: None,
            html_path: None,
            reason: None,
            bytes: Some(body.len() as u64),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_qr_with_watermark_writes_png() {
        let tmp = std::env::temp_dir();
        let plain = tmp.join("upi_test_plain.png");
        let wm = tmp.join("upi_test_wm.png");
        let _ = std::fs::remove_file(&plain);
        let _ = std::fs::remove_file(&wm);

        render_qr_png("upi://pay?pa=test@bank&am=1", &plain, None).unwrap();
        render_qr_png("upi://pay?pa=test@bank&am=1", &wm, Some("@prr9293")).unwrap();

        assert!(std::fs::metadata(&plain).unwrap().len() > 100);
        assert!(std::fs::metadata(&wm).unwrap().len() > 100);

        let plain_img = image::open(&plain).unwrap();
        let wm_img = image::open(&wm).unwrap();

        // Cả 2 phải có cùng width (canvas dựa trên QR width).
        assert_eq!(plain_img.width(), wm_img.width());

        // Watermark version có pixel đen ở phần dưới (text rendered).
        // Lấy 30 hàng cuối, kiểm tra có ít nhất 1 pixel đen.
        let luma = wm_img.to_luma8();
        let (w, h) = luma.dimensions();
        let mut dark_count = 0u32;
        for y in (h.saturating_sub(30))..h {
            for x in 0..w {
                if luma.get_pixel(x, y).0[0] < 128 {
                    dark_count += 1;
                }
            }
        }
        assert!(
            dark_count > 0,
            "watermark text not rendered (no dark pixels in bottom band)"
        );

        std::fs::remove_file(&plain).ok();
        std::fs::remove_file(&wm).ok();
    }
}
