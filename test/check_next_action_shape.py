#!/usr/bin/env python3
"""Đọc research log probe thành công → in cấu trúc next_action.upi_... để
thiết kế extractor hosted_instructions_url + qr."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "runtime" / "research_logs" / "upi_qr_probe_20260616-215951.json"


def walk(o, path="$"):
    if isinstance(o, dict):
        if "upi_handle_redirect_or_display_qr_code" in o:
            print(f"[FOUND] path={path}", flush=True)
            block = o["upi_handle_redirect_or_display_qr_code"]
            print(json.dumps(block, indent=2)[:2000], flush=True)
            # in luôn các key cha (next_action.type?)
            print(f"[parent keys] {list(o.keys())}", flush=True)
            return True
        for k, v in o.items():
            if walk(v, f"{path}.{k}"):
                return True
    elif isinstance(o, list):
        for i, v in enumerate(o):
            if walk(v, f"{path}[{i}]"):
                return True
    return False


def main() -> int:
    if not LOG.exists():
        print(f"[skip] không thấy {LOG}", flush=True)
        return 2
    data = json.loads(LOG.read_text(encoding="utf-8"))
    if not walk(data):
        print("[miss] không tìm thấy upi_handle_redirect_or_display_qr_code", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
