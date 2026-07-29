"""Probe Camoufox AsyncCamoufox class internals + persistent_context handling."""
from __future__ import annotations

import inspect
from pathlib import Path

# Find AsyncCamoufox class source
from camoufox import async_api
print("async_api file:", async_api.__file__)

from camoufox.async_api import AsyncCamoufox
print()
print("AsyncCamoufox class methods:")
for n in dir(AsyncCamoufox):
    if not n.startswith("_"):
        print(f"  - {n}")
print()
print("AsyncCamoufox __init__ source:")
try:
    src = inspect.getsource(AsyncCamoufox.__init__)
    print(src)
except Exception as exc:
    print("getsource failed:", exc)

print()
print("AsyncCamoufox __aenter__ source:")
try:
    src = inspect.getsource(AsyncCamoufox.__aenter__)
    print(src)
except Exception as exc:
    print("getsource failed:", exc)

# Check if persistent_context kwarg is honored
print()
print("Trying instantiate with persistent_context=False:")
try:
    cf = AsyncCamoufox(headless=True, persistent_context=False)
    print("  OK, instance attrs:", {k: v for k, v in cf.__dict__.items() if not k.startswith("_") and k != "launch_options"})
except Exception as exc:
    print("  FAILED:", type(exc).__name__, exc)
