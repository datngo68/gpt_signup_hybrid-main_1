"""Sanitized live probe for ViOTP service catalog; never rents a number."""
from pathlib import Path
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_auth.sms import ViotpProvider
from db import get_engine
from db.repositories import SettingsRepository

values = dotenv_values(ROOT / ".dev.var")
repo = SettingsRepository(get_engine())
key = repo.get("codex.sms_api_key") or values.get("AIPKEY_SMS")
configured = str(repo.get("codex.sms_service_id") or "1234").strip()
if not key:
    print("CATALOG outcome=config-missing")
    raise SystemExit(2)

try:
    services = ViotpProvider(api_key=key, service_id=configured).get_services()
    matches = [s for s in services if "openai" in str(s.get("name") or "").lower() or "chatgpt" in str(s.get("name") or "").lower()]
    configured_match = any(str(s.get("id") or "").strip() == configured for s in matches)
    field_names = sorted({str(k) for s in matches for k in s.keys() if str(k).lower() not in {"token", "api_key", "apikey"}})
    print(f"CATALOG outcome=ok services={len(services)} target_matches={len(matches)} configured_match={str(configured_match).lower()} target_fields={','.join(field_names)}")
except Exception as exc:
    print(f"CATALOG outcome=failed error_type={type(exc).__name__}")
    raise SystemExit(1)
