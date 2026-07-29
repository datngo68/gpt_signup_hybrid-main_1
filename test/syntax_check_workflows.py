#!/usr/bin/env python3
"""Validate YAML syntax cho GitHub Actions workflow files.

Chạy:
    .venv/bin/python3 test/syntax_check_workflows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def main() -> int:
    if not WORKFLOW_DIR.is_dir():
        print(f"[FAIL] {WORKFLOW_DIR} không tồn tại", flush=True)
        return 1

    yml_files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    if not yml_files:
        print(f"[INFO] Không có workflow file trong {WORKFLOW_DIR}", flush=True)
        return 0

    failures = 0
    for idx, path in enumerate(yml_files, start=1):
        rel = path.relative_to(ROOT)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"[FAIL] [{idx}/{len(yml_files)}] {rel} :: YAMLError: {exc}", flush=True)
            failures += 1
            continue

        # Minimal sanity: phải là dict + có 'jobs' key.
        if not isinstance(data, dict):
            print(f"[FAIL] [{idx}/{len(yml_files)}] {rel} :: root không phải dict", flush=True)
            failures += 1
            continue
        if "jobs" not in data:
            print(f"[FAIL] [{idx}/{len(yml_files)}] {rel} :: thiếu 'jobs' key", flush=True)
            failures += 1
            continue

        n_jobs = len(data["jobs"])
        print(f"[PASS] [{idx}/{len(yml_files)}] {rel} ({n_jobs} jobs)", flush=True)

    if failures:
        print(f"\n{failures}/{len(yml_files)} failed", flush=True)
        return 1
    print(f"\nAll {len(yml_files)} workflow files OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
