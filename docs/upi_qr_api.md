# UPI QR Local API

Tai lieu nay danh cho nguoi can goi flow UPI QR bang HTTP local, khong can mo web UI.
Sau khi doc xong, ban co the start API server, POST chuoi account, va nhan anh QR PNG.

## Scope

API nay la server local rieng cho flow UPI QR da debug:

- Login account bang `account_line`.
- Tao checkout voi promo mac dinh.
- Neu promo khong ve free offer thi dung som.
- Confirm UPI QR.
- Retry approve mac dinh 100 lan, delay 3 giay.
- Approve dung 1 proxy cho 3 request roi moi doi proxy.
- Neu backend tra `result=exception` qua nguong thi dung som va bao loi.
- Tra ve anh QR truc tiep neu thanh cong.

Server nay chi nen bind loopback `127.0.0.1` vi request co chua credential account.

## Start Server

```powershell
python3 test\upi_qr_api_server.py --host 127.0.0.1 --port 8091
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8091/health
```

Expected:

```json
{"ok": true}
```

## Create QR

Endpoint:

```text
POST /upi-qr
Content-Type: application/json
```

Minimal request:

```json
{
  "account_line": "email|password|totp_secret"
}
```

PowerShell example:

```powershell
$body = @{
  account_line = 'email|password|totp_secret'
} | ConvertTo-Json

Invoke-WebRequest `
  -Uri 'http://127.0.0.1:8091/upi-qr' `
  -Method POST `
  -ContentType 'application/json' `
  -Body $body `
  -OutFile 'runtime\research_logs\api_upi_qr.png'
```

Success response body is the QR image:

```text
200 OK
Content-Type: image/png
```

Useful response headers:

```text
X-UPI-QR-Account
X-UPI-QR-Amount
X-UPI-QR-Approve
X-UPI-QR-Artifact
X-UPI-QR-Source-Url
X-UPI-QR-Backend-Exception-Count
X-UPI-QR-Backend-Exception-Threshold
```

## Request Options

All fields except `account_line` are optional.

```json
{
  "account_line": "email|password|totp_secret",
  "promo": true,
  "approve_retries": 100,
  "approve_delay": 3,
  "approve_proxy_batch": 3,
  "approve_backend_exception_fails": 2,
  "timeout_seconds": 3600,
  "checkout_proxy_url": null
}
```

Defaults:

```text
promo=true
approve_retries=100
approve_delay=3
approve_proxy_batch=3
approve_backend_exception_fails=2
timeout_seconds=3600
checkout_proxy_url=null
```

Proxy policy:

- Checkout uses current network by default.
- If `checkout_proxy_url` is set, checkout uses that proxy.
- Stripe init / confirm / approve use `proxy.pool` from Settings Store.
- Approve uses each proxy for `approve_proxy_batch` requests, then switches to the next proxy.
- Approve stops early if `result=exception` reaches `approve_backend_exception_fails`.

## Error Responses

Promo enabled but amount is not free:

```text
409 Conflict
```

```json
{
  "ok": false,
  "error": "no free offer",
  "account": "masked@email",
  "amount": "199900 (INR 1999.00)",
  "artifact": "runtime research log path"
}
```

Approve backend exception threshold:

```text
502 Bad Gateway
```

```json
{
  "ok": false,
  "error": "approve backend_exception threshold",
  "account": "masked@email",
  "amount": "0 (INR 0.00)",
  "artifact": "runtime research log path",
  "backend_exception_count": "2",
  "backend_exception_threshold": "2",
  "last_approve": "exception attempt=2 http=200 proxy=http://***@host:port"
}
```

Upstream or flow failure:

```text
502 Bad Gateway
```

```json
{
  "ok": false,
  "error": "qr not found",
  "account": "masked@email",
  "amount": "0 (INR 0.00)",
  "artifact": "runtime research log path"
}
```

Flow timeout:

```text
504 Gateway Timeout
```

## Notes

- Full flow detail is saved in the artifact path returned by the API.
- The API returns only the QR image on success. Metadata is available through response headers and artifact JSON.
- Do not expose this API on LAN/public networks unless you add external authentication.
