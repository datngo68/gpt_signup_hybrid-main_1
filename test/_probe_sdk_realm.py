"""Probe whether sdk.js (real OpenAI sentinel SDK) works when installed via
different paths.

Goal: confirm if the Xray error is from sdk.js internal failure-path or
from genuine cross-realm TypedArray access.
"""
from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel_browser import (
    SENTINEL_SDK_URL, _verify_sdk_patch_markers,
    _build_install_script,
    _in_page_script_path,
)


async def fetch_sdk(ctx):
    resp = await ctx.request.get(
        SENTINEL_SDK_URL,
        headers={
            "accept": "*/*",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
        },
    )
    return await resp.text()


async def main():
    from camoufox.async_api import AsyncCamoufox

    in_page_script = _in_page_script_path().read_text()

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

        # Go to chatgpt.com first to get sentinel cookies
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        print("loaded chatgpt.com")

        sdk_text = await fetch_sdk(ctx)
        print(f"sdk.js fetched: {len(sdk_text)} bytes")
        _verify_sdk_patch_markers(sdk_text, log=print)

        # ── Attempt A: page.evaluate with sdk eval inside ──
        print("\n=== A: page.evaluate eval(sdk) ===")
        wrapper_a = (
            "async (args) => {\n"
            + in_page_script + "\n"
            "  return await __runSentinelInPage(args);\n"
            "}"
        )
        try:
            r = await page.evaluate(wrapper_a, {
                "sdkSource": sdk_text,
                "payload": {"action": "requirements", "device_id": "did-test-A"},
            })
            print(f"  OK: request_p={(r.get('request_p') or '')[:60]}")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {str(e)[:200]}")

        # ── Attempt B: add_init_script install + page.evaluate call ──
        print("\n=== B: add_init_script install + evaluate call ===")
        ctx2 = await br.new_context()
        install_script = _build_install_script(in_page_script, sdk_text)
        await ctx2.add_init_script(install_script)
        page2 = await ctx2.new_page()
        await page2.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        ready = await page2.evaluate("() => !!globalThis.__sentinel_inpage_ready")
        print(f"  installed via add_init_script: ready={ready}")
        try:
            r = await page2.evaluate(
                "async (payload) => {\n"
                "  const r = await globalThis.__runSentinelInPage({\n"
                "    sdkSource: globalThis.__sentinel_sdk_source,\n"
                "    payload,\n"
                "  });\n"
                "  return JSON.parse(JSON.stringify(r));\n"
                "}",
                {"action": "requirements", "device_id": "did-test-B"},
            )
            print(f"  OK: request_p={(r.get('request_p') or '')[:60]}")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {str(e)[:200]}")
        await ctx2.close()

        # ── Attempt C: add_script_tag(content=) WITH inline content ──
        print("\n=== C: page.add_script_tag(content=install_script) ===")
        ctx3 = await br.new_context()
        page3 = await ctx3.new_page()
        await page3.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        try:
            await page3.add_script_tag(content=install_script)
            ready = await page3.evaluate("() => !!globalThis.__sentinel_inpage_ready")
            print(f"  installed via script_tag: ready={ready}")
            err = await page3.evaluate("() => globalThis.__sentinel_inpage_error || null")
            if err:
                print(f"  install error: {err}")
            r = await page3.evaluate(
                "async (payload) => {\n"
                "  const r = await globalThis.__runSentinelInPage({\n"
                "    sdkSource: globalThis.__sentinel_sdk_source,\n"
                "    payload,\n"
                "  });\n"
                "  return JSON.parse(JSON.stringify(r));\n"
                "}",
                {"action": "requirements", "device_id": "did-test-C"},
            )
            print(f"  OK: request_p={(r.get('request_p') or '')[:60]}")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {str(e)[:200]}")
        await ctx3.close()

        # ── Attempt D: navigate to about:blank first (no CSP) ──
        print("\n=== D: about:blank base + add_script_tag ===")
        ctx4 = await br.new_context()
        page4 = await ctx4.new_page()
        await page4.goto("about:blank", wait_until="domcontentloaded")
        try:
            await page4.add_script_tag(content=install_script)
            ready = await page4.evaluate("() => !!globalThis.__sentinel_inpage_ready")
            print(f"  installed: ready={ready}")
            err = await page4.evaluate("() => globalThis.__sentinel_inpage_error || null")
            if err:
                print(f"  install error: {err}")
            if ready:
                r = await page4.evaluate(
                    "async (payload) => {\n"
                    "  const r = await globalThis.__runSentinelInPage({\n"
                    "    sdkSource: globalThis.__sentinel_sdk_source,\n"
                    "    payload,\n"
                    "  });\n"
                    "  return JSON.parse(JSON.stringify(r));\n"
                    "}",
                    {"action": "requirements", "device_id": "did-test-D"},
                )
                print(f"  OK: request_p={(r.get('request_p') or '')[:60]}")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {str(e)[:200]}")
        await ctx4.close()

        # ── Attempt E: expose_function callback ──
        print("\n=== E: expose_function callback ===")
        ctx5 = await br.new_context()
        page5 = await ctx5.new_page()

        result_holder = {}
        async def receive(payload):
            result_holder["got"] = payload

        await page5.expose_function("__pySentinelResult", receive)
        # Install: include code that calls __pySentinelResult
        await ctx5.add_init_script(install_script + """
            (async () => {
                if (!globalThis.__sentinel_inpage_ready) return;
                try {
                    const r = await globalThis.__runSentinelInPage({
                        sdkSource: globalThis.__sentinel_sdk_source,
                        payload: {action: 'requirements', device_id: 'did-test-E'},
                    });
                    await globalThis.__pySentinelResult(JSON.stringify({ok: true, result: r}));
                } catch (e) {
                    await globalThis.__pySentinelResult(JSON.stringify({ok: false, error: String(e)}));
                }
            })();
        """)
        await page5.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        # Wait for callback
        for i in range(50):
            if "got" in result_holder:
                break
            await asyncio.sleep(0.1)
        if "got" in result_holder:
            data = json.loads(result_holder["got"])
            if data.get("ok"):
                print(f"  OK via callback: request_p={(data['result'].get('request_p') or '')[:60]}")
            else:
                print(f"  FAILED via callback: {data.get('error', '')[:200]}")
        else:
            print("  TIMEOUT — callback not fired")
        await ctx5.close()


asyncio.run(main())
