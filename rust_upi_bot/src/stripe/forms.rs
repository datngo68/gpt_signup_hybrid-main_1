//! Stripe form encoding — flatten nested dict thành bracket notation.
//!
//! Port từ `pay_upi_http.py::_to_form` + `_flatten`.
//!
//! Convention Stripe (giống PHP `http_build_query`):
//!   {"a": {"b": "x"}}     → a[b]=x
//!   {"items": [1, 2]}     → items[0]=1&items[1]=2
//!   None                  → skip key (KHÔNG send)
//!   bool                  → "true"/"false"

use serde_json::Value;

/// Flatten 1 nested JSON value với prefix → list of (key, value) pairs.
pub fn flatten(prefix: &str, value: &Value, out: &mut Vec<(String, String)>) {
    match value {
        Value::Null => {}
        Value::Bool(b) => out.push((prefix.to_string(), if *b { "true" } else { "false" }.to_string())),
        Value::Number(n) => out.push((prefix.to_string(), n.to_string())),
        Value::String(s) => out.push((prefix.to_string(), s.clone())),
        Value::Array(arr) => {
            for (i, v) in arr.iter().enumerate() {
                flatten(&format!("{}[{}]", prefix, i), v, out);
            }
        }
        Value::Object(obj) => {
            for (k, v) in obj {
                flatten(&format!("{}[{}]", prefix, k), v, out);
            }
        }
    }
}

/// Convert top-level JSON object → form-urlencoded pairs.
pub fn to_form(data: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    if let Value::Object(obj) = data {
        for (k, v) in obj {
            flatten(k, v, &mut out);
        }
    }
    out
}


#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Reference output từ `test/check_stripe_form_parity.py`.
    #[test]
    fn parity_with_python_to_form() {
        let payload = json!({
            "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1",
            "expected_amount": 0,
            "expected_payment_method_type": "upi",
            "passive_captcha_token": null,
            "passive_captcha_ekey": null,
            "elements_session_client": {
                "client_betas": ["beta_a", "beta_b"],
                "is_aggregation_expected": "false",
                "locale": "en"
            },
            "payment_method_data": {
                "billing_details": {
                    "address": {
                        "city": "Mumbai",
                        "country": "IN",
                        "postal_code": "400001"
                    },
                    "email": "test@example.com",
                    "name": "Aarav Sharma"
                },
                "type": "upi",
                "upi": {"qr_code": {}}
            },
            "version": "e5ebd5e1e6"
        });

        let pairs = to_form(&payload);
        let expected: Vec<(&str, &str)> = vec![
            ("_stripe_version", "2025-03-31.basil; checkout_server_update_beta=v1"),
            ("expected_amount", "0"),
            ("expected_payment_method_type", "upi"),
            ("elements_session_client[client_betas][0]", "beta_a"),
            ("elements_session_client[client_betas][1]", "beta_b"),
            ("elements_session_client[is_aggregation_expected]", "false"),
            ("elements_session_client[locale]", "en"),
            ("payment_method_data[billing_details][address][city]", "Mumbai"),
            ("payment_method_data[billing_details][address][country]", "IN"),
            ("payment_method_data[billing_details][address][postal_code]", "400001"),
            ("payment_method_data[billing_details][email]", "test@example.com"),
            ("payment_method_data[billing_details][name]", "Aarav Sharma"),
            ("payment_method_data[type]", "upi"),
            ("version", "e5ebd5e1e6"),
        ];
        assert_eq!(pairs.len(), expected.len(), "pairs count mismatch: {:#?}", pairs);
        for ((k, v), (ek, ev)) in pairs.iter().zip(expected.iter()) {
            assert_eq!(k, ek);
            assert_eq!(v, ev);
        }
    }
}
