"""Probe Camoufox version + AsyncCamoufox signature on this machine."""
from __future__ import annotations

import inspect

try:
    import camoufox
    print("camoufox module:", camoufox.__file__)
except ImportError as exc:
    print("camoufox not installed:", exc)
    raise SystemExit(1)

try:
    from camoufox.async_api import AsyncCamoufox
    sig = inspect.signature(AsyncCamoufox.__init__)
    print("AsyncCamoufox.__init__:", sig)
    print()
    print("Parameters:")
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        print(f"  - {name}: default={p.default!r}")
except Exception as exc:
    print("inspect AsyncCamoufox failed:", exc)

try:
    from camoufox.utils import launch_options
    sig = inspect.signature(launch_options)
    print()
    print("launch_options:", sig)
except Exception as exc:
    print("inspect launch_options failed:", exc)

# Test direct call to see which kwargs are accepted
print()
print("Trying minimal launch_options(...) call to verify accepted kwargs:")
try:
    from camoufox.utils import launch_options as _lo
    out = _lo()  # all defaults
    print("  empty call OK, returned keys:", sorted(out.keys()))
except Exception as exc:
    print("  empty call FAILED:", type(exc).__name__, exc)

# Check if fingerprint_preset is supported
print()
try:
    from camoufox.utils import launch_options as _lo
    out = _lo(fingerprint_preset=True)
    print("  fingerprint_preset=True accepted (kwarg present)")
except TypeError as exc:
    if "fingerprint_preset" in str(exc):
        print("  fingerprint_preset NOT supported in this Camoufox version:", exc)
    else:
        print("  TypeError (other):", exc)
except Exception as exc:
    print("  fingerprint_preset check raised:", type(exc).__name__, exc)
