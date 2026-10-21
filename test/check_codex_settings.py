# test/check_codex_settings.py
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import unittest
from db.repositories import _EXACT_KEYS, _SENSITIVE_KEYS, _validate_type_constraint, RepositoryError

class TestCodexSettings(unittest.TestCase):
    def test_keys_exist(self):
        self.assertIn("codex.sms_provider", _EXACT_KEYS)
        self.assertIn("codex.sms_api_key", _EXACT_KEYS)
        self.assertIn("codex.sms_service_id", _EXACT_KEYS)
        self.assertIn("codex.mode", _EXACT_KEYS)
        self.assertIn("codex.sms_api_key", _SENSITIVE_KEYS)

    def test_validation(self):
        # Valid cases
        _validate_type_constraint("codex.sms_provider", "viotp")
        _validate_type_constraint("codex.sms_provider", "chothuesimcode")
        _validate_type_constraint("codex.sms_provider", "")
        _validate_type_constraint("codex.sms_provider", None)

        _validate_type_constraint("codex.mode", "api")
        _validate_type_constraint("codex.mode", "browser")
        _validate_type_constraint("codex.mode", None)

        _validate_type_constraint("codex.sms_api_key", "my-secret-key")
        _validate_type_constraint("codex.sms_api_key", None)

        _validate_type_constraint("codex.sms_service_id", "1")
        _validate_type_constraint("codex.sms_service_id", None)

        # Invalid cases
        with self.assertRaises(RepositoryError):
            _validate_type_constraint("codex.sms_provider", "invalid_provider")

        with self.assertRaises(RepositoryError):
            _validate_type_constraint("codex.mode", "invalid_mode")

        with self.assertRaises(RepositoryError):
            _validate_type_constraint("codex.sms_provider", 123)

        with self.assertRaises(RepositoryError):
            _validate_type_constraint("codex.sms_api_key", "a" * 300)

        with self.assertRaises(RepositoryError):
            _validate_type_constraint("codex.sms_service_id", 999)

if __name__ == "__main__":
    unittest.main()
