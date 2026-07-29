"""Verify Camoufox main_world_eval kwarg works."""
from __future__ import annotations
import asyncio


async def main():
    from camoufox.async_api import AsyncCamoufox
    try:
        async with AsyncCamoufox(
            headless=True,
            main_world_eval=True,
            persistent_context=False,
        ) as br:
            ctx = await br.new_context()
            page = await ctx.new_page()
            await page.goto("https://example.com", wait_until="domcontentloaded")
            # Try evaluating a function that uses TypedArray.
            result = await page.evaluate(
                "() => {"
                "  const arr = new Uint8Array(4);"
                "  crypto.getRandomValues(arr);"
                "  return { bytes: Array.from(arr), len: arr.length };"
                "}"
            )
            print("main_world_eval=True OK:", result)
            await ctx.close()
        print("PASSED")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        raise


asyncio.run(main())
