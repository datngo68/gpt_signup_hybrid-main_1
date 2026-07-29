"""Dummy long-sleep process cho test reaper.

Tên/arg chứa marker ('camoufox'/'firefox') để ``BrowserProcessReaper`` nhận
diện là process browser. IGNORE SIGTERM để mô phỏng browser WEDGED (không
phản hồi SIGTERM) → buộc reaper escalate SIGKILL (đường hard-kill quan trọng
nhất cho bug orphan). Chỉ sleep — KHÔNG làm gì khác.
"""
import signal
import sys
import time

# argv[1] (vd "camoufox-reaper-dummy") làm command-line chứa marker.
_label = sys.argv[1] if len(sys.argv) > 1 else "camoufox-dummy"
_ = _label

# Mô phỏng process wedged: nuốt SIGTERM → reaper phải SIGKILL.
signal.signal(signal.SIGTERM, signal.SIG_IGN)

while True:
    time.sleep(3600)
