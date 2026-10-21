from __future__ import annotations

import json
import re
import sys
from urllib.request import Request, urlopen


BASE = "http://127.0.0.1:18083"


def main() -> None:
    job_id = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else ""
    html = urlopen(BASE + "/", timeout=10).read().decode("utf-8", "replace")
    match = re.search(r'name="auth-token"\s+content="([^"]+)"', html)
    if not match:
        raise SystemExit("NO_TOKEN")
    req = Request(
        f"{BASE}/api/session/jobs/{job_id}/log",
        headers={"X-API-Token": match.group(1)},
    )
    data = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
    text = "\n".join(data.get("log", [])[-120:])
    if out_path:
        from pathlib import Path
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
