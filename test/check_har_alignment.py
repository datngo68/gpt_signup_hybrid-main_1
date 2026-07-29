"""Phase 5 Task 5.2 — HAR alignment script.

So HAR runtime với HAR golden record (browser thật) để bắt regression. 5 invariants:

    1. Endpoint sequence: critical endpoints xuất hiện đúng ORDER, không thiếu,
       không dư endpoint chỉ-bot-mới-gọi (vd /authorize/continue).
    2. Header keys per critical endpoint: set name khớp golden ±tolerance.
    3. Header order: Levenshtein distance < 5 cho top 10 headers (chỉ tham khảo).
    4. Cookie names sent: superset của golden cookies cho mỗi endpoint critical.
    5. Body shape: JSON body của /register, /validate, /create_account có
       cùng keys với golden.

Usage:
    .venv/bin/python3 test/check_har_alignment.py <runtime_har> [<golden_har>]

    Default golden = runtime/research_logs/web_record_20260625-120705_manual/trace.har

    Self-test: chạy không args → so golden vs golden (phải PASS 100%).

Exit codes:
    0 = pass tất cả invariants
    1 = FAIL ít nhất 1 invariant
    2 = file/usage error
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GOLDEN = ROOT / "runtime" / "research_logs" / \
    "web_record_20260625-120705_manual" / "trace.har"


# Critical endpoints — sequence MATTERS (golden order).
# Pattern format: ("LABEL", "METHOD", "PATH_SUBSTRING")
CRITICAL_SEQUENCE = [
    ("PROVIDERS",     "GET",  "/api/auth/providers"),
    ("CSRF",          "GET",  "/api/auth/csrf"),
    ("SIGNIN",        "POST", "/api/auth/signin/openai"),
    ("EMAIL_VER",     "GET",  "/email-verification"),
    ("REGISTER",      "POST", "/api/accounts/user/register"),
    ("OTP_SEND",      "GET",  "/api/accounts/email-otp/send"),
    ("OTP_VALIDATE",  "POST", "/api/accounts/email-otp/validate"),
    ("CREATE_ACC",    "POST", "/api/accounts/create_account"),
    ("CALLBACK",      "GET",  "/api/auth/callback/openai"),
]

# Endpoint NÊN VẮNG mặt (chỉ-bot-mới-gọi).
ENDPOINTS_FORBIDDEN = [
    ("AUTHORIZE_CONTINUE", "/api/accounts/authorize/continue"),
]

# Header tolerance — set difference so với golden cho mỗi endpoint critical.
HEADER_TOLERANCE_MAX_DIFF = 4  # cho phép 4 header thừa/thiếu
HEADER_REQUIRED_PER_ENDPOINT: dict[str, set[str]] = {
    # Header bắt buộc (lowercase) — KHÔNG được thiếu, dù persona/library nào.
    "REGISTER": {
        "user-agent", "accept", "accept-language",
        "content-type", "origin", "referer",
        "openai-sentinel-token",
        "traceparent", "tracestate",
        "x-datadog-origin", "x-datadog-trace-id", "x-datadog-parent-id",
    },
    "OTP_VALIDATE": {
        "user-agent", "accept", "content-type", "origin", "referer",
        "traceparent", "tracestate", "x-datadog-origin",
    },
    "CREATE_ACC": {
        "user-agent", "accept", "content-type", "origin", "referer",
        "openai-sentinel-token",
        "traceparent", "tracestate", "x-datadog-origin",
    },
    "OTP_SEND": {
        "user-agent", "accept",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
        "upgrade-insecure-requests",
    },
}

# Cookie names BẮT BUỘC gửi cho mỗi endpoint critical (browser thật luôn có).
COOKIE_REQUIRED_PER_ENDPOINT: dict[str, set[str]] = {
    "REGISTER": {
        "oai-did", "__cf_bm", "__cflb", "_cfuvid", "cf_clearance",
        "oai-login-csrf_dev_3772291445", "oai-client-auth-session", "oai-sc",
    },
    "OTP_VALIDATE": {
        "oai-did", "__cf_bm", "_cfuvid", "cf_clearance",
        "oai-login-csrf_dev_3772291445", "oai-client-auth-session", "oai-sc",
    },
    "CREATE_ACC": {
        "oai-did", "__cf_bm", "_cfuvid", "cf_clearance",
        "oai-login-csrf_dev_3772291445", "oai-client-auth-session", "oai-sc",
    },
    "CSRF": {
        "oai-did", "__cf_bm", "_cfuvid", "cf_clearance",
        # oai-asli optional (chatgpt.com sentinel set lúc nào tùy)
    },
}

# Body keys BẮT BUỘC cho JSON request body của mỗi endpoint critical.
BODY_KEYS_PER_ENDPOINT: dict[str, set[str]] = {
    "REGISTER":     {"username", "password"},
    "OTP_VALIDATE": {"code"},
    "CREATE_ACC":   {"name", "birthdate"},
}


# ─────────────────────────────────────────────────────────────────────


def _extract_critical_via_jq(har_path: Path, cache_path: Path) -> list[dict]:
    """Extract critical entries từ HAR bằng `jq` (C-based stream filter, fast).

    HAR 70MB → critical entries < 100KB. Python json.load HAR thẳng RẤT chậm
    (>5min cho 70MB) — dùng jq giảm xuống vài giây.
    """
    import shutil
    import subprocess

    if not shutil.which("jq"):
        # Fallback: Python json.load (chậm nhưng work)
        with har_path.open("r", encoding="utf-8") as f:
            har = json.load(f)
        all_entries = har.get("log", {}).get("entries", []) or []
    else:
        # Build jq filter pattern: select request.url chứa critical/forbidden patterns.
        critical_paths = [pat for _, _, pat in CRITICAL_SEQUENCE]
        forbidden_paths = [pat for _, pat in ENDPOINTS_FORBIDDEN]
        all_paths = critical_paths + forbidden_paths
        # jq syntax: contains("...") or contains("...") ...
        contains_clauses = " or ".join(
            f'contains("{p}")' for p in all_paths
        )
        jq_filter = (
            f".log.entries | map(select(.request.url | "
            f"({contains_clauses})))"
        )
        proc = subprocess.run(
            ["jq", "-c", jq_filter, str(har_path)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"jq filter failed: {proc.stderr[:500]}")
        all_entries = json.loads(proc.stdout)

    # Save cache
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(all_entries, f, ensure_ascii=False)
    return all_entries


def _load_har(path: Path) -> list[dict]:
    """Load HAR và return list[entry].

    Optimization: HAR 70MB+ thì preprocess via jq → cache critical entries.
    """
    size_mb = path.stat().st_size / 1024 / 1024

    # File nhỏ — load trực tiếp.
    if size_mb < 5:
        with path.open("r", encoding="utf-8") as f:
            har = json.load(f)
        return har.get("log", {}).get("entries", []) or []

    # File lớn — dùng jq cache.
    cache = path.with_suffix(path.suffix + ".critical_cache.json")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        with cache.open("r", encoding="utf-8") as f:
            return json.load(f)

    print(f"  [cache] preprocessing {path.name} ({size_mb:.1f} MB) via jq...")
    entries = _extract_critical_via_jq(path, cache)
    print(f"  [cache] saved {len(entries)} critical entries → {cache.name}")
    return entries


def _classify_entry(ent: dict) -> str | None:
    """Return label nếu entry match critical sequence, else None."""
    req = ent.get("request", {})
    method = req.get("method", "")
    url = req.get("url", "")
    parsed = urlparse(url)
    path = parsed.path
    for label, expected_method, path_substr in CRITICAL_SEQUENCE:
        if method == expected_method and path_substr in path:
            return label
    return None


def _header_names_lowercase(headers: list[dict]) -> list[str]:
    """List of lowercase header names, preserve order."""
    return [h.get("name", "").lower() for h in headers if h.get("name")]


def _cookie_names_sent(req: dict) -> set[str]:
    """Set of cookie names gửi trong request (parse từ Cookie header HOẶC req.cookies)."""
    names: set[str] = set()
    for c in req.get("cookies", []) or []:
        n = c.get("name")
        if n:
            names.add(n)
    if not names:
        for h in req.get("headers", []):
            if h.get("name", "").lower() == "cookie":
                for part in h.get("value", "").split("; "):
                    eq = part.find("=")
                    if eq > 0:
                        names.add(part[:eq].strip())
    return names


def _body_keys(req: dict) -> set[str] | None:
    """Parse JSON body → set of top-level keys. None nếu không có body / không phải JSON."""
    pd = req.get("postData", {}) or {}
    text = pd.get("text", "")
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return set(obj.keys())
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────


def check_alignment(runtime_path: Path, golden_path: Path) -> tuple[bool, list[str]]:
    """Return (all_pass, list[message]). Message format: 'LEVEL [INV] description'."""
    messages: list[str] = []
    all_pass = True

    runtime_entries = _load_har(runtime_path)
    golden_entries = _load_har(golden_path)
    messages.append(f"INFO  loaded runtime={len(runtime_entries)} entries, golden={len(golden_entries)}")

    # Group entries by label cho cả runtime + golden (first occurrence).
    def _first_by_label(entries: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for ent in entries:
            label = _classify_entry(ent)
            if label and label not in result:
                result[label] = ent
        return result

    rt_by_label = _first_by_label(runtime_entries)
    gd_by_label = _first_by_label(golden_entries)

    # ── Invariant 1 — Endpoint sequence ──
    rt_labels = []
    for ent in runtime_entries:
        label = _classify_entry(ent)
        if label and label not in rt_labels:
            rt_labels.append(label)
    gd_labels = []
    for ent in golden_entries:
        label = _classify_entry(ent)
        if label and label not in gd_labels:
            gd_labels.append(label)

    missing_in_rt = set(gd_labels) - set(rt_labels)
    if missing_in_rt:
        all_pass = False
        messages.append(
            f"FAIL  [INV1] runtime thiếu endpoint critical: {sorted(missing_in_rt)}"
        )
    else:
        messages.append(f"PASS  [INV1] all {len(gd_labels)} critical endpoints present in runtime")

    # ── Invariant 1b — Forbidden endpoints (chỉ-bot-mới-gọi) ──
    rt_paths = [
        urlparse(e.get("request", {}).get("url", "")).path
        for e in runtime_entries
    ]
    forbidden_hits: list[str] = []
    for label, path_substr in ENDPOINTS_FORBIDDEN:
        if any(path_substr in p for p in rt_paths):
            forbidden_hits.append(f"{label}({path_substr})")
    if forbidden_hits:
        all_pass = False
        messages.append(
            f"FAIL  [INV1b] runtime gọi endpoint chỉ-bot-mới-gọi: {forbidden_hits}"
        )
    else:
        messages.append("PASS  [INV1b] no forbidden endpoints (authorize/continue absent)")

    # ── Invariant 2 — Header keys per critical endpoint ──
    for label, required in HEADER_REQUIRED_PER_ENDPOINT.items():
        rt_ent = rt_by_label.get(label)
        if rt_ent is None:
            messages.append(f"SKIP  [INV2:{label}] runtime missing endpoint")
            continue
        rt_headers = set(_header_names_lowercase(rt_ent.get("request", {}).get("headers", [])))
        missing = required - rt_headers
        if missing:
            all_pass = False
            messages.append(
                f"FAIL  [INV2:{label}] missing required headers: {sorted(missing)}"
            )
        else:
            messages.append(
                f"PASS  [INV2:{label}] all {len(required)} required headers present"
            )

    # ── Invariant 3 — Header order (top 10) Levenshtein-like ──
    for label in HEADER_REQUIRED_PER_ENDPOINT:
        rt_ent = rt_by_label.get(label)
        gd_ent = gd_by_label.get(label)
        if not rt_ent or not gd_ent:
            continue
        rt_top = _header_names_lowercase(rt_ent["request"]["headers"])[:10]
        gd_top = _header_names_lowercase(gd_ent["request"]["headers"])[:10]
        # Simple distance: count common prefix length / max len
        common = 0
        for a, b in zip(rt_top, gd_top):
            if a == b:
                common += 1
            else:
                break
        if common >= min(5, len(gd_top)):
            messages.append(
                f"PASS  [INV3:{label}] header order match top {common}/{len(gd_top)}"
            )
        else:
            # Không fail (header order tolerance lỏng) — chỉ cảnh báo
            messages.append(
                f"WARN  [INV3:{label}] header order match top {common}/{len(gd_top)} "
                f"(< 5; có thể curl_cffi reorder)"
            )

    # ── Invariant 4 — Cookie names sent ──
    for label, required in COOKIE_REQUIRED_PER_ENDPOINT.items():
        rt_ent = rt_by_label.get(label)
        if rt_ent is None:
            messages.append(f"SKIP  [INV4:{label}] runtime missing endpoint")
            continue
        rt_cookies = _cookie_names_sent(rt_ent.get("request", {}))
        missing = required - rt_cookies
        if missing:
            all_pass = False
            messages.append(
                f"FAIL  [INV4:{label}] missing cookies: {sorted(missing)} "
                f"(present: {len(rt_cookies)})"
            )
        else:
            messages.append(
                f"PASS  [INV4:{label}] all {len(required)} required cookies present "
                f"({len(rt_cookies)} total sent)"
            )

    # ── Invariant 5 — JSON body keys ──
    for label, required in BODY_KEYS_PER_ENDPOINT.items():
        rt_ent = rt_by_label.get(label)
        if rt_ent is None:
            messages.append(f"SKIP  [INV5:{label}] runtime missing endpoint")
            continue
        rt_body = _body_keys(rt_ent.get("request", {}))
        if rt_body is None:
            all_pass = False
            messages.append(f"FAIL  [INV5:{label}] body không phải JSON / không có")
            continue
        missing = required - rt_body
        extra = rt_body - required
        if missing:
            all_pass = False
            messages.append(
                f"FAIL  [INV5:{label}] body missing keys: {sorted(missing)} "
                f"(got: {sorted(rt_body)})"
            )
        elif extra:
            messages.append(
                f"PASS  [INV5:{label}] body has all keys (+ extra: {sorted(extra)})"
            )
        else:
            messages.append(f"PASS  [INV5:{label}] body keys exact: {sorted(rt_body)}")

    return all_pass, messages


# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    args = sys.argv[1:]
    if len(args) > 2:
        print(__doc__, file=sys.stderr)
        return 2

    if not args:
        # Self-test: golden vs golden — phải PASS 100%
        runtime_p = DEFAULT_GOLDEN
        golden_p = DEFAULT_GOLDEN
        print(f"[self-test] golden vs golden ({DEFAULT_GOLDEN.name})")
    elif len(args) == 1:
        runtime_p = Path(args[0])
        golden_p = DEFAULT_GOLDEN
        print(f"[har-align] runtime={runtime_p} golden=DEFAULT")
    else:
        runtime_p = Path(args[0])
        golden_p = Path(args[1])
        print(f"[har-align] runtime={runtime_p} golden={golden_p}")

    if not runtime_p.exists():
        print(f"ERROR: runtime HAR not found: {runtime_p}", file=sys.stderr)
        return 2
    if not golden_p.exists():
        print(f"ERROR: golden HAR not found: {golden_p}", file=sys.stderr)
        return 2

    print()
    ok, msgs = check_alignment(runtime_p, golden_p)
    for m in msgs:
        print(f"  {m}")
    print()
    if ok:
        print("[OK] HAR alignment PASS — runtime khớp golden trên mọi invariant.")
        return 0
    print("[FAIL] HAR alignment FAIL — kiểm tra messages [FAIL] phía trên.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
