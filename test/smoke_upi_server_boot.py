"""Smoke test: khởi động FastAPI app + verify endpoints UPI trả 200/401 đúng.

Chạy:
    .venv/bin/python test/smoke_upi_server_boot.py

KHÔNG gọi `/api/upi/jobs` POST (sẽ tốn account thật). Chỉ verify:
    - App startup OK (engine + managers init)
    - GET /api/upi/jobs với token OK trả snapshot
    - GET /api/upi/config với token OK trả default values
    - GET /api/upi/jobs không token trả 401
    - GET /api/upi/jobs/{nonexistent}/qr trả 404 (không crash server)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Set env trước khi import server để DB nằm trong tmp dir → tránh ô nhiễm runtime/data.db.
_TMPDIR = tempfile.mkdtemp(prefix="gsh_upi_smoke_")
_TMP_DB = str(Path(_TMPDIR) / "data.db")
os.environ["GSH_DB_PATH"] = _TMP_DB
os.environ["GPT_SIGNUP_WEB_TOKEN"] = "test-token-smoke"

# Pre-init DatabaseEngine singleton tới tmp path. Server.py sẽ gọi get_engine()
# không args → trả singleton đã pre-init này.
sys.path.insert(0, str(ROOT.parent))
from gpt_signup_hybrid_new.db import get_engine as _get_engine_init  # noqa: E402

_get_engine_init(db_path=_TMP_DB)

PASSED = 0
FAILED = 0


def _check(label: str, fn) -> None:
    global PASSED, FAILED
    try:
        fn()
        print(f"[PASS] {label}", flush=True)
        PASSED += 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[FAIL] {label} :: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        FAILED += 1


def main() -> int:
    from fastapi.testclient import TestClient
    from gpt_signup_hybrid_new.web import server as srv

    client = TestClient(srv.app)
    headers = {"X-API-Token": "test-token-smoke"}

    def t01_jobs_list_with_token():
        r = client.get("/api/upi/jobs", headers=headers)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "jobs" in data
        assert "max_concurrent" in data
        assert "approve_retries" in data
        assert data["max_concurrent"] >= 1
        assert data["approve_retries"] >= 1

    def t02_jobs_list_no_token_401():
        r = client.get("/api/upi/jobs")
        assert r.status_code == 401, f"expect 401 got {r.status_code}"

    def t03_config_get():
        r = client.get("/api/upi/config", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["max_concurrent"] >= 1
        assert data["job_timeout"] >= 60
        assert data["approve_retries"] >= 1

    def t04_config_set_writethrough():
        r = client.post(
            "/api/upi/config",
            headers=headers,
            json={"max_concurrent": 5, "approve_retries": 200, "job_timeout": 600},
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        data = r.json()
        assert data["max_concurrent"] == 5
        assert data["approve_retries"] == 200
        assert data["job_timeout"] == 600
        # Verify settings store cũng đã ghi.
        from gpt_signup_hybrid_new.db.repositories import SettingsRepository
        repo = SettingsRepository(srv._engine)  # type: ignore[arg-type]
        assert repo.get("upi.max_concurrent") == 5
        assert repo.get("upi.approve_retries") == 200
        assert repo.get("upi.job_timeout") == 600

    def t05_config_validation_400():
        r = client.post(
            "/api/upi/config", headers=headers, json={"max_concurrent": 100},
        )
        # Pydantic Field(le=50) → 422 Unprocessable Entity
        assert r.status_code in (400, 422), f"expect 4xx got {r.status_code}: {r.text[:200]}"

    def t06_qr_nonexistent_404():
        r = client.get("/api/upi/jobs/zzzznotexist/qr", headers=headers)
        assert r.status_code == 404, f"expect 404 got {r.status_code}"

    def t07_log_nonexistent_404():
        r = client.get("/api/upi/jobs/zzzznotexist/log", headers=headers)
        assert r.status_code == 404

    def t08_clear_finished_ok():
        r = client.post("/api/upi/jobs/clear-finished", headers=headers)
        assert r.status_code == 200
        assert "removed" in r.json()

    def t09_stop_all_ok():
        r = client.post("/api/upi/jobs/stop-all", headers=headers)
        assert r.status_code == 200
        assert "stopped" in r.json()

    def t10_html_serves_upi_tab():
        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        assert 'data-tab="upi"' in body
        assert 'id="tab-upi"' in body
        assert 'multi20' in body and 'multi30' in body and 'multi50' in body

    def t11_static_upi_js():
        r = client.get("/static/upi.js")
        assert r.status_code == 200
        assert "upi-combo-input" in r.text

    def t12_add_invalid_combo_creates_error_job():
        # Add format sai → tạo job error ngay (không gọi API thật).
        r = client.post(
            "/api/upi/jobs", headers=headers,
            json={"combos": "no_pipe_line\n# comment\n\n"},
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        data = r.json()
        # 1 invalid line → 1 job error (mgr add_jobs vẫn create job error).
        assert data["added"] >= 1
        # Cleanup
        for j in data["jobs"]:
            client.delete(f"/api/upi/jobs/{j['id']}", headers=headers)

    tests = [
        ("01 GET /api/upi/jobs với token", t01_jobs_list_with_token),
        ("02 GET /api/upi/jobs không token → 401", t02_jobs_list_no_token_401),
        ("03 GET /api/upi/config", t03_config_get),
        ("04 POST /api/upi/config + write-through Settings", t04_config_set_writethrough),
        ("05 POST /api/upi/config invalid → 4xx", t05_config_validation_400),
        ("06 GET /api/upi/jobs/{?}/qr → 404", t06_qr_nonexistent_404),
        ("07 GET /api/upi/jobs/{?}/log → 404", t07_log_nonexistent_404),
        ("08 POST /api/upi/jobs/clear-finished", t08_clear_finished_ok),
        ("09 POST /api/upi/jobs/stop-all", t09_stop_all_ok),
        ("10 GET / serve HTML kèm tab UPI", t10_html_serves_upi_tab),
        ("11 GET /static/upi.js", t11_static_upi_js),
        ("12 POST /api/upi/jobs với combo sai → job error", t12_add_invalid_combo_creates_error_job),
    ]

    with client:
        for label, fn in tests:
            _check(label, fn)

    print(f"\n--- summary: {PASSED} pass, {FAILED} fail ---", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
