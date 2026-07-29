"""Probe different ways to bypass Firefox Xray + run sdk.js in main realm."""
from __future__ import annotations
import asyncio


CANARY_SCRIPT = r"""
async function probeXray() {
  // Reproduce sdk.js pattern: create canvas, read pixel data (TypedArray).
  const c = document.createElement('canvas');
  c.width = 200; c.height = 50;
  const ctx = c.getContext('2d');
  ctx.fillStyle = 'red';
  ctx.fillRect(0, 0, 200, 50);
  const data = ctx.getImageData(0, 0, 200, 50);
  // The Uint8ClampedArray .data — if Xray blocks, reading [0] errors.
  let sum = 0;
  for (let i = 0; i < Math.min(data.data.length, 100); i++) sum += data.data[i];
  return { canvas_ok: true, sum, len: data.data.length };
}
return probeXray();
"""


async def main():
    from camoufox.async_api import AsyncCamoufox

    print("=== Test 1: persistent_context=False + main_world_eval=True + page.evaluate ===")
    async with AsyncCamoufox(
        headless=True,
        main_world_eval=True,
        persistent_context=False,
    ) as br:
        ctx = await br.new_context()
        page = await ctx.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        try:
            r = await page.evaluate("async () => {\n" + CANARY_SCRIPT + "\n}")
            print(f"  page.evaluate: {r}")
        except Exception as e:
            print(f"  page.evaluate: FAILED {type(e).__name__}: {str(e)[:120]}")
        await ctx.close()

    print()
    print("=== Test 2: page.add_script_tag(content=...) ===")
    async with AsyncCamoufox(
        headless=True,
        main_world_eval=True,
        persistent_context=False,
    ) as br:
        ctx = await br.new_context()
        page = await ctx.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        # Install a global function via add_script_tag (creates <script> element)
        await page.add_script_tag(content="""
            globalThis.__probeMainRealm = async function() {
                const c = document.createElement('canvas');
                c.width = 200; c.height = 50;
                const ctx = c.getContext('2d');
                ctx.fillStyle = 'red';
                ctx.fillRect(0, 0, 200, 50);
                const data = ctx.getImageData(0, 0, 200, 50);
                let sum = 0;
                for (let i = 0; i < Math.min(data.data.length, 100); i++) sum += data.data[i];
                return { canvas_ok: true, sum, len: data.data.length, realm: 'main' };
            };
        """)
        try:
            r = await page.evaluate("async () => await globalThis.__probeMainRealm()")
            print(f"  add_script_tag → evaluate: {r}")
        except Exception as e:
            print(f"  add_script_tag → evaluate: FAILED {type(e).__name__}: {str(e)[:200]}")
        await ctx.close()

    print()
    print("=== Test 3: add_init_script + page.evaluate ===")
    async with AsyncCamoufox(
        headless=True,
        main_world_eval=True,
        persistent_context=False,
    ) as br:
        ctx = await br.new_context()
        await ctx.add_init_script("""
            globalThis.__probeMainRealm = async function() {
                const c = document.createElement('canvas');
                c.width = 200; c.height = 50;
                const ctx = c.getContext('2d');
                ctx.fillStyle = 'red';
                ctx.fillRect(0, 0, 200, 50);
                const data = ctx.getImageData(0, 0, 200, 50);
                let sum = 0;
                for (let i = 0; i < Math.min(data.data.length, 100); i++) sum += data.data[i];
                return { canvas_ok: true, sum, len: data.data.length, realm: 'init_script' };
            };
        """)
        page = await ctx.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        try:
            r = await page.evaluate("async () => await globalThis.__probeMainRealm()")
            print(f"  add_init_script → evaluate: {r}")
        except Exception as e:
            print(f"  add_init_script → evaluate: FAILED {type(e).__name__}: {str(e)[:200]}")
        await ctx.close()

    print()
    print("=== Test 4: page.expose_function (Python callback) ===")
    async with AsyncCamoufox(
        headless=True,
        main_world_eval=True,
        persistent_context=False,
    ) as br:
        ctx = await br.new_context()
        page = await ctx.new_page()

        result_holder = {}
        async def receive(payload):
            result_holder["got"] = payload
            return "ack"

        await page.expose_function("__pyReceive", receive)
        await page.goto("https://example.com", wait_until="domcontentloaded")

        # Use <script> tag for main realm execution
        await page.add_script_tag(content="""
            (async () => {
                const c = document.createElement('canvas');
                c.width = 200; c.height = 50;
                const ctx = c.getContext('2d');
                ctx.fillStyle = 'red';
                ctx.fillRect(0, 0, 200, 50);
                const data = ctx.getImageData(0, 0, 200, 50);
                let sum = 0;
                for (let i = 0; i < Math.min(data.data.length, 100); i++) sum += data.data[i];
                const payload = { canvas_ok: true, sum, len: data.data.length, realm: 'script_tag' };
                // Send to Python via exposed function (cross-realm safe)
                await globalThis.__pyReceive(JSON.stringify(payload));
            })();
        """)
        # Wait for callback
        for _ in range(20):
            if "got" in result_holder:
                break
            await asyncio.sleep(0.1)
        print(f"  Python callback received: {result_holder.get('got')}")
        await ctx.close()


asyncio.run(main())
