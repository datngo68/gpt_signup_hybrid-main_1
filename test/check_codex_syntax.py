# test/check_codex_syntax.py
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import codex_auth.runner
import codex_auth.browser
import codex_auth.__main__
import codex_auth.sms
import codex_auth.api_runner

print("All codex_auth modules imported successfully!")
