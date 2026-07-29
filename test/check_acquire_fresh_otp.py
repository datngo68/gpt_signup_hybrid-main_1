"""Kiểm chứng hành vi _acquire_fresh_otp (request_phase) — mirror vòng poll OTP
của browser_phase: multi-code, stale-skip, resend có giới hạn, timeout.

Mock mail_provider + monkeypatch _step_resend_otp/_step_send_otp + time.sleep
(no-op để bỏ các sleep 5s/2s, monotonic vẫn thật cho deadline).

Chạy: python3 test/check_acquire_fresh_otp.py
In [PASS]/[FAIL] từng TC ngay khi xong (flush realtime).
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import request_phase as rp  # noqa: E402

_failures = 0


def _log(msg: str) -> None:
    # log của flow — im lặng để output test gọn; bật nếu cần debug.
    pass


def _make_request(**over) -> types.SimpleNamespace:
    base = dict(
        source_email=None,
        email="x@icloud.com",
        otp_resend_after_seconds=0.1,  # threshold ~[0.05, 0.1]s → resend gần như ngay
        otp_poll_interval_seconds=0.0,  # floor 5.0 nhưng time.sleep no-op
        otp_timeout_seconds=10.0,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class FakeMail:
    """poll_otp trả lần lượt theo script; poll_all_codes trả map theo code."""

    def __init__(self, otp_script: list[str], all_codes_map: dict[str, list[str]] | None = None):
        self._script = list(otp_script)
        self._all_map = all_codes_map or {}
        self.poll_calls = 0
        self.all_calls = 0

    async def poll_otp(self, *, recipient, started_at, timeout_seconds, poll_interval_seconds, log):
        self.poll_calls += 1
        await asyncio.sleep(0.005)  # mô phỏng I/O + để monotonic tiến (deadline)
        if self._script:
            return self._script.pop(0)
        return ""

    async def poll_all_codes(self, *, recipient, started_at, log):
        self.all_calls += 1
        # trả theo code mới nhất vừa nhận (last popped). Đơn giản: dùng key "*".
        return list(self._all_map.get("*", []))


class StaleUntilResendMail:
    """poll_otp trả code cũ (stale) cho tới khi resend được gọi, sau đó trả code mới.

    Mô phỏng đúng bug: account mới mailbox chỉ có 1 code; code mới chỉ xuất hiện
    SAU khi resend (resend_counter tăng).
    """

    def __init__(self, stale_code: str, new_code: str, resend_counter: dict):
        self.stale = stale_code
        self.new = new_code
        self._rc = resend_counter
        self._baseline = resend_counter["n"]
        self.poll_calls = 0
        self.all_calls = 0

    async def poll_otp(self, *, recipient, started_at, timeout_seconds, poll_interval_seconds, log):
        self.poll_calls += 1
        await asyncio.sleep(0.005)
        if self._rc["n"] > self._baseline:
            return self.new
        return self.stale

    async def poll_all_codes(self, *, recipient, started_at, log):
        self.all_calls += 1
        return [self.new] if self._rc["n"] > self._baseline else []


def _result(tc: str, ok: bool, detail: str) -> None:
    global _failures
    tag = "PASS" if ok else "FAIL"
    if not ok:
        _failures += 1
    print(f"[{tag}] {tc} :: {detail}", flush=True)


def run() -> int:
    # Patch network + sleep (lưu để restore)
    orig_resend = rp._step_resend_otp
    orig_send = rp._step_send_otp
    orig_sleep = time.sleep

    resend_calls = {"n": 0}
    send_calls = {"n": 0}

    def fake_resend(session, device_id, log):
        resend_calls["n"] += 1
        return True

    def fake_send(session, device_id, log):
        send_calls["n"] += 1

    rp._step_resend_otp = fake_resend
    rp._step_send_otp = fake_send
    time.sleep = lambda *_a, **_k: None  # no-op mọi sleep trong rp (cùng module time)

    # Kiểm soát ngưỡng resend per-TC (helper floor base ở 10s nên không thể ép nhỏ
    # qua config). thr["v"]=0 → resend ngay khi chờ; lớn → không resend trong test.
    orig_uniform = rp.random.uniform
    thr = {"v": 100.0}
    rp.random.uniform = lambda a, b: thr["v"]

    loop = asyncio.new_event_loop()
    started = rp.datetime.now(rp.timezone.utc)
    try:
        # TC-01: 1 code về ngay
        mail = FakeMail(["111111"], {"*": ["111111"]})
        pending: list[str] = []
        tried: set[str] = set()
        code, used = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail, request=_make_request(),
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=1,
        )
        _result("TC-01 single-code", code == "111111" and used == 0 and pending == [],
                 f"code={code} used={used} pending={pending}")

        # TC-02: multi-code → pending nạp code dư; lần 2 pop pending không poll mạng
        mail = FakeMail(["222222"], {"*": ["222222", "333333"]})
        pending = []
        tried = set()
        code1, u1 = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail, request=_make_request(),
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=1,
        )
        ok_first = code1 == "222222" and pending == ["333333"]
        tried.add(code1)
        polls_before = mail.poll_calls
        code2, u2 = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail, request=_make_request(),
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=1,
        )
        ok_second = code2 == "333333" and mail.poll_calls == polls_before  # không poll mạng
        _result("TC-02 multi-code pending", ok_first and ok_second,
                 f"code1={code1} pending_after={['done'] if ok_first else pending} code2={code2} "
                 f"extra_polls={mail.poll_calls - polls_before}")

        # TC-03: stale code (đã thử) rồi mới có code mới — KHÔNG resend (chưa tới ngưỡng)
        thr["v"] = 100.0  # ngưỡng cao → không resend trong test nhanh
        mail = FakeMail(["444444", "555555"], {"*": ["555555"]})
        pending = []
        tried = {"444444"}
        code, used = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail, request=_make_request(),
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=1,
        )
        _result("TC-03 stale-then-new", code == "555555" and used == 0,
                 f"code={code} used={used} poll_calls={mail.poll_calls}")

        # TC-04: rỗng kéo dài → resend theo ngưỡng (ép ngưỡng=0)
        thr["v"] = 0.0  # resend ngay khi chờ (waited >= 0)
        resend_calls["n"] = 0
        send_calls["n"] = 0

        mail = FakeMail(["", "", "666666"], {"*": ["666666"]})
        pending = []
        tried = set()
        code, used = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail,
            request=_make_request(otp_resend_after_seconds=0.001),  # threshold ~ms → resend sớm
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=1,
        )
        _result(
            "TC-04 resend-on-empty",
            code == "666666" and used == 1 and resend_calls["n"] == 1,
            f"code={code} used={used} resend_calls={resend_calls['n']}",
        )

        # TC-05: luôn rỗng + max_resends=0 → timeout raise, KHÔNG resend
        resend_calls["n"] = 0
        send_calls["n"] = 0
        mail = FakeMail([], {})  # luôn "" 
        pending = []
        tried = set()
        raised = False
        try:
            rp._acquire_fresh_otp(
                session=None, device_id="d", mail_provider=mail,
                request=_make_request(otp_timeout_seconds=0.3),
                log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
                max_resends=0,
            )
        except rp.RequestPhaseError:
            raised = True
        _result("TC-05 timeout-no-resend", raised and resend_calls["n"] == 0,
                 f"raised={raised} resend_calls={resend_calls['n']}")

        # TC-06: BUG thực tế — mailbox trả mãi code cũ (stale), chỉ có code mới SAU
        # khi resend. Phải resend rồi lấy được code mới (không chờ suông tới chết).
        thr["v"] = 0.0  # ép resend ngay khi gặp stale
        resend_calls["n"] = 0
        send_calls["n"] = 0
        mail = StaleUntilResendMail("046616", "999999", resend_calls)
        pending = []
        tried = {"046616"}  # code cũ đã verify sai
        code, used = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail,
            request=_make_request(otp_resend_after_seconds=0.001),
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=3,
        )
        _result("TC-06 stale-forever-resend",
                 code == "999999" and used == 1 and resend_calls["n"] == 1,
                 f"code={code} used={used} resend_calls={resend_calls['n']}")

        # TC-07: prefer_second_code — có 2 mã ở lần fetch đầu → submit mã THỨ 2
        # trước, mã đầu giữ pending fallback.
        thr["v"] = 100.0  # không resend
        mail = FakeMail(["AAAAAA"], {"*": ["AAAAAA", "BBBBBB"]})
        pending = []
        tried = set()
        code, used = rp._acquire_fresh_otp(
            session=None, device_id="d", mail_provider=mail, request=_make_request(),
            log=_log, loop=loop, started_at=started, tried_codes=tried, pending=pending,
            max_resends=0, prefer_second_code=True,
        )
        ok_07 = code == "BBBBBB" and pending == ["AAAAAA"] and used == 0
        _result("TC-07 prefer-second-code", ok_07,
                 f"code={code} pending={pending} used={used}")

    finally:
        loop.close()
        rp._step_resend_otp = orig_resend
        rp._step_send_otp = orig_send
        rp.random.uniform = orig_uniform
        time.sleep = orig_sleep

    total = 7
    print(f"--- {total - _failures}/{total} PASS ---", flush=True)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(run())
