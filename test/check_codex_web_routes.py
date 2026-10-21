# test/check_codex_web_routes.py
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Đặt token cố định cho test
TEST_TOKEN = "test-codex-token-12345"
os.environ["GPT_SIGNUP_WEB_TOKEN"] = TEST_TOKEN

import unittest
from fastapi.testclient import TestClient
from web.server import app
from db.engine import DatabaseEngine
from db.repositories import SettingsRepository
from web import server
import tempfile

class TestCodexWebRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp_dir.name, "test_data.db")
        cls.engine = DatabaseEngine(cls.db_path)
        cls.client = TestClient(app)

        server._engine = cls.engine
        server._settings_repo = SettingsRepository(cls.engine)
        cls.headers = {"X-API-Token": TEST_TOKEN}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.engine.close()
        except Exception:
            pass
        try:
            cls.tmp_dir.cleanup()
        except Exception:
            pass

    def test_get_and_set_config(self):
        from unittest.mock import patch
        with patch("codex_auth.sms.get_env_sms_key", return_value=None):
            # 1. Ban đầu trống khi không có env
            res = self.client.get("/api/codex/sms/config", headers=self.headers)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("provider", data)
            self.assertIn("api_key", data)
            self.assertIn("service_id", data)
            self.assertFalse(data["configured"])

            # 2. Lưu config mới
            save_res = self.client.post("/api/codex/sms/config", headers=self.headers, json={
                "provider": "viotp",
                "api_key": "dummy_mock_key_12345",
                "service_id": "1234"
            })
            self.assertEqual(save_res.status_code, 200)
            self.assertTrue(save_res.json()["configured"])

            # 3. Đọc lại kiểm tra
            res2 = self.client.get("/api/codex/sms/config", headers=self.headers)
            self.assertEqual(res2.status_code, 200)
            data2 = res2.json()
            self.assertEqual(data2["provider"], "viotp")
            self.assertEqual(data2["api_key"], "dummy_mock_key_12345")
            self.assertEqual(data2["service_id"], "1234")
            self.assertTrue(data2["configured"])

    def test_test_endpoint_validation(self):
        from unittest.mock import patch
        with patch("codex_auth.sms.get_env_sms_key", return_value=None):
            # Khi không có key, trả về 400
            res = self.client.post("/api/codex/sms/test", headers=self.headers, json={"api_key": ""})
            self.assertEqual(res.status_code, 400)

if __name__ == "__main__":
    unittest.main()
