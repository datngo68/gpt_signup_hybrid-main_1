# Golden HAR records — anti-ban validation reference

Phase 5 (journal `260625-1224-reg-anti-ban-master-plan.md`) dùng HAR record
của browser thật (signup successful) làm "ground truth" để validate runtime
HAR không có regression.

## Active golden record

**Path** (cố định, KHÔNG copy vào repo do file 70MB):
```
runtime/research_logs/web_record_20260625-120705_manual/trace.har
```

- **Captured**: 2026-06-25 12:07
- **Browser**: Camoufox = Firefox 135 Mac
- **Email**: `toy-clink.2g+y788y7y877@icloud.com`
- **Profile**: VIPPRO MAX, birthdate 2004-06-25
- **Status**: full signup successful (account created + chatgpt.com session)
- **Critical entries**: 9 (8 sequence + 0 forbidden)

Re-record khi:
- OpenAI thay đổi auth flow / endpoint mới.
- Sentinel SDK version bump (xem `SENTINEL_VERSION` trong `sentinel_quickjs.py`).
- 3 tháng / lần (defensive — protocol drift).

## Self-test

```bash
.venv/bin/python3 test/check_har_alignment.py
```

→ self-test golden vs golden, must PASS 18/18 invariants.

## Validate runtime HAR

```bash
.venv/bin/python3 test/check_har_alignment.py runtime/har_hybrid/<your-runtime>.har
```

Hoặc auto-validate sau mỗi reg debug:
```bash
.venv/bin/python3 -m gpt_signup_hybrid signup \
    --email <combo> \
    --har \
    --har-validate
```

## Cache

HAR 70MB → script tự pre-extract critical entries qua `jq` rồi cache vào
`<har>.critical_cache.json` (~150KB). Cache invalidated khi mtime của HAR
mới hơn cache.

## 5 Invariants

1. **Endpoint sequence**: 8 critical endpoints xuất hiện đúng order.
2. **Header keys per endpoint**: required headers (sentinel-token, datadog,
   sec-fetch-*) đầy đủ.
3. **Header order**: top 10 headers match golden (info, không fail).
4. **Cookie names sent**: superset cookies bắt buộc (oai-did, __cf_bm,
   oai-sc, oai-login-csrf_dev_*, ...).
5. **Body shape**: JSON body keys của `/register`, `/validate`,
   `/create_account` chính xác.
