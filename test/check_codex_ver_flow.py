"""Offline regression for one-rental and secret-safe phone verification."""
import asyncio
import ast
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_auth import browser
from codex_auth.errors import PhoneVerificationRequired


async def check_browser_rejection():
    provider = Mock()
    provider.request_phone.return_value = ("private-request-id", "0912345678")
    provider.cancel.return_value = True
    page = Mock()
    page.url = "https://auth.openai.com/add-phone"
    page.keyboard.press = AsyncMock()
    page.inner_text = AsyncMock(return_value="Unsupported phone number")
    field = Mock()
    field.click = AsyncMock()
    field.type = AsyncMock()
    logs = []
    with patch.object(browser, "_first_visible", AsyncMock(side_effect=[None, field, None])), patch.object(
        browser, "_click_first", AsyncMock(return_value=True)
    ), patch.object(browser.asyncio, "sleep", AsyncMock()):
        try:
            await browser.handle_phone_verification(page, provider, log=logs.append)
        except PhoneVerificationRequired:
            pass
        else:
            raise AssertionError("Rejected phone did not fail fast")
    assert provider.request_phone.call_count == 1
    provider.cancel.assert_called_with("private-request-id")
    output = "\n".join(logs)
    assert "private-request-id" not in output
    assert "0912345678" not in output


def check_api_source():
    source = (ROOT / "codex_auth" / "api_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_codex_auth_api")
    rental_loops = [n for n in ast.walk(func) if isinstance(n, ast.For)
                    and any(isinstance(c, ast.Attribute) and c.attr == "request_phone" for c in ast.walk(n))]
    assert len(rental_loops) == 1
    loop = rental_loops[0]
    assert isinstance(loop.iter, ast.Call) and getattr(loop.iter.func, "id", "") == "range"
    assert [a.value for a in loop.iter.args if isinstance(a, ast.Constant)] == [1, 2]
    for forbidden in ("req_id=", "Thuê số {full_phone}", "Gửi code tới số {full_phone}"):
        assert forbidden not in source


if __name__ == "__main__":
    asyncio.run(check_browser_rejection())
    check_api_source()
    print("PASS one-rental fail-fast and secret-safe phone logs")
