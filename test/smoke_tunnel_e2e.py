"""E2E smoke test: bật/tắt Cloudflare Quick Tunnel qua HTTP API.

Yêu cầu:
  - .venv đã active, uvicorn cài đầy đủ.
  - Có Internet (để cloudflared tải + tạo tunnel).
  - Web server CHƯA chạy ở port 8089 (dùng port riêng tránh đụng UI dev).

Flow:
  1. Spawn uvicorn ở port 8089 trong process con (background).
  2. Đợi /api/tunnel/status trả 200 (server ready).
  3. POST /api/tunnel/config {enabled: true} → verify URL.
  4. GET /api/tunnel/status → verify status=running.
  5. POST /api/tunnel/config {enabled: false} → verify status=stopped.
  6. Kill uvicorn.

Chạy: python3 test/smoke_tunnel_e2e.py
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8089
HOST = "127.0.0.1"
BASE = f"http://{HOST}:{PORT}"
PYTHON = sys.executable
STARTUP_TIMEOUT = 30
TUNNEL_START_TIMEOUT = 60  # gồm cả tải binary lần đầu


def step(label: str, ok: bool, detail: str = "") -> None:
    flag = "[PASS]" if ok else "[FAIL]"
    line = f"{flag} {label}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)
    if not ok:
        raise SystemExit(1)


def http(path: str, *, method: str = "GET", body: dict | None = None,
         token: str = "") -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-API-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TUNNEL_START_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "null")


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def wait_server(timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(HOST, PORT):
            return
        time.sleep(0.3)
    raise TimeoutError(f"server không lên trong {timeout}s")


def get_token() -> str:
    """Đọc token từ Settings DB / env (web.auth.get_token logic).

    Vì server cần token để bypass auth middleware, copy từ
    `GPT_SIGNUP_WEB_TOKEN` hoặc đọc qua DB.
    """
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("RUNTIME_DIR", str(ROOT / "runtime"))
    from web.auth import get_token as _g
    return _g()


def main() -> None:
    if port_open(HOST, PORT):
        step(f"port {PORT} free", False, "đang có process khác chiếm")

    token = get_token()
    step("got auth token", bool(token), token[:6] + "…" if token else "")

    # Spawn uvicorn ở chính venv hiện tại (gpt_signup_hybrid web command).
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        PYTHON, "-m", "gpt_signup_hybrid", "web",
        "--host", HOST, "--port", str(PORT),
    ]
    print(f"[spawn] {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(sys.platform != "win32"),
    )
    try:
        wait_server(STARTUP_TIMEOUT)
        step("uvicorn server up", True, BASE)

        # 1. Status ban đầu — tunnel mặc định off.
        code, snap = http("/api/tunnel/status", token=token)
        step("GET /api/tunnel/status", code == 200, f"code={code} snap={snap}")
        step("initial status stopped/disabled",
             snap.get("status") in ("stopped",) and snap.get("enabled") is False,
             repr(snap))

        # 2. Bật tunnel.
        print("[enable] đang bật tunnel — có thể tải cloudflared lần đầu (~30s)…", flush=True)
        t0 = time.time()
        code, snap = http("/api/tunnel/config", method="POST",
                          body={"enabled": True}, token=token)
        elapsed = int(time.time() - t0)
        step(f"POST enable=true ({elapsed}s)", code == 200, f"snap={snap}")
        step("status running", snap.get("status") == "running", snap.get("status"))
        step("URL trycloudflare.com",
             isinstance(snap.get("url"), str) and snap["url"].endswith(".trycloudflare.com"),
             snap.get("url"))
        step("enabled=true", snap.get("enabled") is True)

        url = snap["url"]
        print(f"[tunnel-url] {url}", flush=True)

        # 3. GET status lần nữa, xác nhận url giữ nguyên.
        code, snap2 = http("/api/tunnel/status", token=token)
        step("status persists URL", snap2.get("url") == url, snap2.get("url"))

        # 4. Tắt tunnel.
        code, snap3 = http("/api/tunnel/config", method="POST",
                           body={"enabled": False}, token=token)
        step("POST enable=false", code == 200, f"snap={snap3}")
        step("status stopped", snap3.get("status") == "stopped", snap3.get("status"))
        step("URL cleared", snap3.get("url") is None)
        step("enabled=false", snap3.get("enabled") is False)

        print("\n[ALL PASS] tunnel E2E OK", flush=True)
    finally:
        print("[cleanup] killing uvicorn…", flush=True)
        try:
            if sys.platform == "win32":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] kill error: {exc}", flush=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
