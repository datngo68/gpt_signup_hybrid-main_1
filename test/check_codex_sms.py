# test/check_codex_sms.py
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import unittest
from codex_auth.sms import (
    clean_phone_vn,
    extract_otp_code,
    create_sms_provider,
    ViotpProvider,
    ChothuesimcodeProvider,
    SmsError,
)

class TestCodexSms(unittest.TestCase):
    def test_clean_phone_vn(self):
        self.assertEqual(clean_phone_vn("0381234567"), ("+84", "381234567"))
        self.assertEqual(clean_phone_vn("84381234567"), ("+84", "381234567"))
        self.assertEqual(clean_phone_vn("+84381234567"), ("+84", "381234567"))
        self.assertEqual(clean_phone_vn("0901234567"), ("+84", "901234567"))

    def test_extract_otp_code(self):
        self.assertEqual(extract_otp_code("Your OpenAI code is 849201"), "849201")
        self.assertEqual(extract_otp_code("Ma xac minh OpenAI cua ban la: 123456"), "123456")
        self.assertEqual(extract_otp_code("OpenAI: 567-890"), "567890")
        self.assertEqual(extract_otp_code("OpenAI: 567 890"), "567890")
        self.assertEqual(extract_otp_code("Khong co ma nao"), None)

    def test_create_provider(self):
        p1 = create_sms_provider("viotp", "test_key")
        self.assertIsInstance(p1, ViotpProvider)
        self.assertEqual(p1.service_id, "1234")

        p2 = create_sms_provider("chothuesimcode", "test_key", "1005")
        self.assertIsInstance(p2, ChothuesimcodeProvider)
        self.assertEqual(p2.service_id, "1005")

        with self.assertRaises(SmsError):
            create_sms_provider("unsupported_service", "key")

if __name__ == "__main__":
    unittest.main()
