from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.server import on_startup


async def main() -> int:
    try:
        await on_startup()
    except BaseException as exc:  # noqa: BLE001 - smoke script must expose startup root cause
        print(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1
    print("STARTUP_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
