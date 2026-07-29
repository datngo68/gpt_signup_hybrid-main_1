"""Process reaper — force-kill Camoufox/Firefox + node driver bị treo.

Lý do tồn tại (bug reliability "multi-signup launch-hang"):
    Khi 1 op Playwright (launch/acquire/mint/close) treo, ``_PlaywrightThread``
    chạy nó trên DAEMON worker — ``run(timeout=...)`` hết hạn sẽ raise
    ``TimeoutError`` ở caller nhưng task vẫn kẹt trên worker (``asyncio.to_thread``
    / ``future.result`` KHÔNG cancel được). Hệ quả: process Firefox/Camoufox
    spawn bởi op đó trở thành ORPHAN, tích lũy qua từng signup tuần tự → cạn
    tài nguyên → signup sau launch chậm/treo.

    ``cm.__exit__`` (close) cũng có thể treo nếu browser wedged → không thực sự
    kill Firefox. Reaper bù lại: SIGTERM → (grace) → SIGKILL các process browser
    là HẬU DUỆ của chính tiến trình này.

An toàn tuyệt đối:
    - CHỈ kill process là DESCENDANT của ``os.getpid()`` (cây tiến trình của
      chính runner/wrapper này) — không bao giờ chạm process ngoài.
    - CHỈ kill process mới xuất hiện SAU ``mark_baseline()`` (delta) — không
      giết browser của signup khác đã chạy từ trước.
    - Tại thời điểm SIGKILL, RE-CHECK lại descendant + tên process → chống
      PID-reuse (PID cũ đã chết, OS cấp lại cho process khác).
    - POSIX only (macOS/Linux qua ``ps``). Non-POSIX → no-op (best-effort).

KHÔNG dùng ``psutil`` (không có trong .venv) — shell ``ps`` stdlib subprocess.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable

# Token nhận diện process browser trong output ``ps`` (lowercase match).
_BROWSER_MARKERS: tuple[str, ...] = ("camoufox", "firefox")
# Node Playwright driver — chỉ kill khi vừa là 'node' vừa thuộc playwright/driver
# (tránh giết node process khác trong cây).
_NODE_MARKERS: tuple[str, ...] = ("playwright", "driver.js", "package/lib/cli")

_PS_TIMEOUT_SECONDS = 8.0


def _is_browser_cmd(cmd: str) -> bool:
    low = cmd.lower()
    if any(m in low for m in _BROWSER_MARKERS):
        return True
    if "node" in low and any(m in low for m in _NODE_MARKERS):
        return True
    return False


def _snapshot_procs() -> dict[int, tuple[int, str]]:
    """Return ``{pid: (ppid, command)}`` cho mọi process (POSIX ``ps``).

    Trả dict rỗng nếu không phải POSIX hoặc ``ps`` lỗi (reaper → no-op).
    """
    if os.name != "posix":
        return {}
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_SECONDS,
        ).stdout
    except Exception:
        return {}
    procs: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        procs[pid] = (ppid, parts[2])
    return procs


def _descendant_browser_pids(root_pid: int) -> dict[int, str]:
    """``{pid: command}`` cho process browser là hậu duệ của ``root_pid``."""
    procs = _snapshot_procs()
    if not procs:
        return {}
    # children map
    children: dict[int, list[int]] = {}
    for pid, (ppid, _cmd) in procs.items():
        children.setdefault(ppid, []).append(pid)
    # BFS descendants của root_pid
    descendants: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in descendants or pid == root_pid:
            continue
        descendants.add(pid)
        stack.extend(children.get(pid, []))
    out: dict[int, str] = {}
    for pid in descendants:
        cmd = procs[pid][1]
        if _is_browser_cmd(cmd):
            out[pid] = cmd
    return out


def _signal(pid: int, sig: int, log: Callable[[str], None]) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # đã chết
    except PermissionError:
        log(f"[hybrid-reaper] no perm to signal pid={pid} (bỏ qua)")
    except Exception as exc:  # noqa: BLE001
        log(f"[hybrid-reaper] signal pid={pid} fail: {exc}")


class BrowserProcessReaper:
    """Theo dõi + force-kill process browser do CHÍNH tiến trình này spawn.

    Vòng đời điển hình:
        reaper = BrowserProcessReaper(log)
        reaper.mark_baseline()        # trước khi launch browser
        ... launch / mint ...
        reaper.kill_new("reason")     # khi teardown timeout / sweep sau close
    """

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._root = os.getpid()
        self._baseline: frozenset[int] = frozenset()

    def mark_baseline(self) -> None:
        """Chốt tập process browser hậu duệ HIỆN CÓ — các process này KHÔNG
        bao giờ bị reaper kill (thuộc signup khác / đã tồn tại từ trước)."""
        self._baseline = frozenset(_descendant_browser_pids(self._root).keys())

    def _new_pids(self) -> dict[int, str]:
        return {
            pid: cmd
            for pid, cmd in _descendant_browser_pids(self._root).items()
            if pid not in self._baseline
        }

    def kill_new(self, reason: str, *, grace_seconds: float = 3.0) -> list[int]:
        """SIGTERM → (grace) → SIGKILL các process browser mới (delta baseline).

        Trả list pid đã SIGKILL. Idempotent + best-effort (không raise).
        """
        targets = self._new_pids()
        if not targets:
            return []
        self._log(
            f"[hybrid-reaper] {reason}: SIGTERM {len(targets)} browser proc(s) "
            f"pids={sorted(targets)}"
        )
        for pid in targets:
            _signal(pid, signal.SIGTERM, self._log)

        # Chờ graceful exit tối đa ``grace_seconds``.
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            alive = set(self._new_pids().keys()) & set(targets.keys())
            if not alive:
                self._log(f"[hybrid-reaper] {reason}: tất cả thoát sau SIGTERM")
                return []
            time.sleep(0.2)

        # SIGKILL các survivor — RE-CHECK descendant browser (chống PID reuse).
        survivors = self._new_pids()
        killed: list[int] = []
        for pid in targets:
            if pid in survivors:
                _signal(pid, signal.SIGKILL, self._log)
                killed.append(pid)
        if killed:
            self._log(f"[hybrid-reaper] {reason}: SIGKILL {sorted(killed)}")
        return killed
