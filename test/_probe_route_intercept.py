"""Approach K2 — let sidecar do REAL form submission, intercept the outgoing
POST /register to extract sentinel-token + so-token from headers, abort
before send, hand tokens back to curl_cffi.

This proves the architecture: real browser fires sdk.js via natural form
submission (same code path as a real user) → tokens are valid + so-token
populated by Session Observer → no Xray issue because no programmatic
sdk.js eval.
"""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from camoufox.async_api import AsyncCamoufox

    captured = {}
    request_event = asyncio.Event()

    async def handle_route(route, request):
        url = request.url
        if "/api/accounts/user/register" in url and request.method == "POST":
            try:
                hdrs = await request.all_headers()
            except Exception:
                hdrs = dict(request.headers)
            captured["url"] = url
            captured["method"] = request.method
            captured["headers"] = hdrs
            captured["body"] = request.post_data
            print(f"  intercepted POST {url}")
            print(f"    body: {captured['body'][:80]!r}")
            print(f"    headers count: {len(hdrs)}")
            for hname in ("openai-sentinel-token", "openai-sentinel-so-token", "openai-sentinel-chat-requirements-token"):
                if hname in hdrs:
                    val = hdrs[hname]
                    print(f"    ✓ {hname}: len={len(val)} prefix={val[:80]!r}")
            request_event.set()
            await route.abort()  # cancel — sidecar never actually sends
        else:
            await route.continue_()

    async with AsyncCamoufox(
        headless=True,
        main_world_eval=True,
        persistent_context=False,
        os=["macos"],
        block_webrtc=True,
        humanize=True,
    ) as br:
        ctx = await br.new_context()
        page = await ctx.new_page()

        # Register route handler BEFORE navigation
        await page.route("**/api/accounts/user/register", handle_route)

        print("Loading chatgpt.com → /email-verification...")
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        # Bootstrap via NextAuth — same as real signup. For probe we
        # navigate authorize URL directly. But simpler: just trigger signup
        # form by going to /email-verification with email pre-filled.
        # The simplest path: navigate to auth.openai.com/email-verification
        # which IF entered fresh shows the email input + "continue with password" button.

        # Actually for testing, just navigate to /create-account/password directly
        # to skip email step.
        try:
            await page.goto(
                "https://auth.openai.com/create-account/password",
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception as e:
            print(f"  goto failed: {e}")

        # Check current URL
        print(f"  current url: {page.url}")

        # Try to find password input (may not exist if redirect)
        try:
            pwd_input = page.locator('input[type="password"]').first
            visible = await pwd_input.is_visible(timeout=3000)
            print(f"  password input visible: {visible}")
            if visible:
                await pwd_input.fill("TestPassword12345@")
                # Find submit button
                for sel in ('button[type="submit"]', 'button:has-text("Continue")'):
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1500):
                        print(f"  clicking submit: {sel}")
                        await btn.click()
                        break
                # Wait for intercept
                try:
                    await asyncio.wait_for(request_event.wait(), timeout=20)
                    print("  ✓ INTERCEPTED!")
                except asyncio.TimeoutError:
                    print(f"  ✗ no intercept after 20s")
        except Exception as e:
            print(f"  form probe failed: {e}")


asyncio.run(main())
