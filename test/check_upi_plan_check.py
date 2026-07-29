"""Smoke check cho feature check-session khi UPI QR hết hạn.

Verify:
  TC-01  syntax: web/upi_runner.py + web/manager.py + web/server.py parse OK.
  TC-02  UpiQrResult có field access_token, session_cookies (default=None).
  TC-03  UpiJob có field _access_token, _session_cookies, plan_check.
  TC-04  to_dict() expose plan_check + can_check_plan, KHÔNG expose
         _access_token / _session_cookies.
  TC-05  UpiJobManager.check_plan tồn tại + là coroutine function.
  TC-06  _extract_plan_from_session: top-level accountPlan + nested
         account.planType + None khi thiếu cả 2.
  TC-07  Endpoint POST /api/upi/jobs/{id}/check-session đã đăng ký.
  TC-08  Frontend upi.js có renderPlanBadge + triggerPlanCheck + hook
         updateCountdowns gọi triggerPlanCheck.
  TC-09  CSS có 3 class upi-plan-{plus,free,err}.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def t01_syntax() -> int:
    targets = [
        ROOT / "web" / "upi_runner.py",
        ROOT / "web" / "manager.py",
        ROOT / "web" / "server.py",
        ROOT / "web" / "static" / "upi.js",  # JS — chỉ check exists
    ]
    for p in targets:
        if not p.exists():
            print(f"[FAIL] TC-01 syntax :: thiếu file {p}", flush=True)
            return 1
        if p.suffix == ".py":
            try:
                _parse(p)
            except SyntaxError as exc:
                print(f"[FAIL] TC-01 syntax :: {p.name} :: {exc}", flush=True)
                return 1
    print("[PASS] TC-01 syntax :: 4 file parse OK", flush=True)
    return 0


def t02_upiqrresult_fields() -> int:
    from gpt_signup_hybrid_new.web.upi_runner import UpiQrResult

    fields = {f for f in UpiQrResult.__dataclass_fields__}
    needed = {"access_token", "session_cookies"}
    missing = needed - fields
    if missing:
        print(f"[FAIL] TC-02 UpiQrResult :: thiếu field {missing}", flush=True)
        return 1
    r = UpiQrResult(ok=False, email="x")
    if r.access_token is not None or r.session_cookies is not None:
        print("[FAIL] TC-02 UpiQrResult :: default phải None", flush=True)
        return 1
    # to_dict KHÔNG được leak credential
    d = r.to_dict()
    leak = {"access_token", "session_cookies"} & set(d.keys())
    if leak:
        print(f"[FAIL] TC-02 UpiQrResult :: to_dict leak {leak}", flush=True)
        return 1
    print("[PASS] TC-02 UpiQrResult :: field + default + to_dict không leak", flush=True)
    return 0


def t03_upijob_fields() -> int:
    from gpt_signup_hybrid_new.web.manager import UpiJob

    fields = {f for f in UpiJob.__dataclass_fields__}
    needed = {"_access_token", "_session_cookies", "plan_check"}
    missing = needed - fields
    if missing:
        print(f"[FAIL] TC-03 UpiJob :: thiếu field {missing}", flush=True)
        return 1
    j = UpiJob(id="x", email="a@b.c", password="p")
    if j._access_token is not None or j._session_cookies is not None or j.plan_check is not None:
        print("[FAIL] TC-03 UpiJob :: default phải None", flush=True)
        return 1
    print("[PASS] TC-03 UpiJob :: field + default", flush=True)
    return 0


def t04_to_dict_shape() -> int:
    from gpt_signup_hybrid_new.web.manager import UpiJob

    j = UpiJob(id="x", email="a@b.c", password="p")
    d = j.to_dict()
    if "plan_check" not in d:
        print("[FAIL] TC-04 to_dict :: thiếu plan_check", flush=True)
        return 1
    if "can_check_plan" not in d:
        print("[FAIL] TC-04 to_dict :: thiếu can_check_plan", flush=True)
        return 1
    if d["can_check_plan"] is not False:
        print(f"[FAIL] TC-04 to_dict :: can_check_plan phải False khi cookies trống, got {d['can_check_plan']!r}", flush=True)
        return 1
    leak = {"_access_token", "_session_cookies"} & set(d.keys())
    if leak:
        print(f"[FAIL] TC-04 to_dict :: leak {leak}", flush=True)
        return 1
    # Khi có cookies giả → can_check_plan = True
    j._session_cookies = [{"name": "x", "value": "y"}]
    if j.to_dict()["can_check_plan"] is not True:
        print("[FAIL] TC-04 to_dict :: can_check_plan phải True khi cookies có", flush=True)
        return 1
    print("[PASS] TC-04 to_dict :: plan_check + can_check_plan + không leak", flush=True)
    return 0


def t05_check_plan_method() -> int:
    from gpt_signup_hybrid_new.web.manager import UpiJobManager

    fn = getattr(UpiJobManager, "check_plan", None)
    if fn is None:
        print("[FAIL] TC-05 check_plan :: method chưa tồn tại", flush=True)
        return 1
    if not asyncio.iscoroutinefunction(fn):
        print("[FAIL] TC-05 check_plan :: phải là async", flush=True)
        return 1
    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())
    if params != ["self", "job_id"]:
        print(f"[FAIL] TC-05 check_plan :: signature lạ {params}", flush=True)
        return 1
    print("[PASS] TC-05 check_plan :: async (self, job_id)", flush=True)
    return 0


def t06_extract_plan() -> int:
    from gpt_signup_hybrid_new.web.manager import _extract_plan_from_session

    cases = [
        ({"accountPlan": "plus"}, "plus"),
        ({"accountPlan": "FREE"}, "free"),
        ({"accountPlan": ""}, None),
        ({"account": {"planType": "team"}}, "team"),
        ({"account": {"planType": "  Plus  "}}, "plus"),
        ({"account": {}}, None),
        ({"account": "wrong-type"}, None),
        ({}, None),
        (None, None),
        # accountPlan top-level ưu tiên hơn nested
        ({"accountPlan": "plus", "account": {"planType": "free"}}, "plus"),
    ]
    for data, expected in cases:
        got = _extract_plan_from_session(data)
        if got != expected:
            print(f"[FAIL] TC-06 extract :: {data!r} → {got!r}, want {expected!r}", flush=True)
            return 1
    print(f"[PASS] TC-06 extract :: {len(cases)} case OK", flush=True)
    return 0


def t07_endpoint_registered() -> int:
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    needles = [
        '@app.post("/api/upi/jobs/{job_id}/check-session")',
        "async def check_upi_job_session",
        "await um.check_plan(job_id)",
    ]
    for n in needles:
        if n not in src:
            print(f"[FAIL] TC-07 endpoint :: thiếu {n!r}", flush=True)
            return 1
    print("[PASS] TC-07 endpoint :: route + handler + delegate", flush=True)
    return 0


def t08_frontend_hooks() -> int:
    src = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")
    needles = [
        "function renderPlanBadge(",
        "function triggerPlanCheck(",
        "/api/upi/jobs/${encodeURIComponent(jobId)}/check-session",
        "${planBadge}",
        "renderPlanBadge(j)",
        # state.jobs là Map → MUST dùng .get(jobId), KHÔNG được [jobId].
        "state.jobs.get(jobId)",
        # Auto-poll: hằng số 20s × 6 + poller completion-driven.
        "const PLAN_POLL_INTERVAL_MS = 20000",
        "const PLAN_POLL_MAX = 6",
        "function startPlanPoll(",
        "function _planPollTick(",
        "function _stopPlanPoll(",
        "clearTimeout(",
        # Nhánh cd.expired gọi startPlanPoll (KHÔNG triggerPlanCheck trực tiếp — chống flood 1 req/giây).
        "startPlanPoll(row.dataset.id)",
        # Nút Recheck: force check kể cả khi đã có plan_check.
        'data-action="recheck-plan"',
        "triggerPlanCheck(id, { force: true })",
        # Poller đếm theo "đã fire thật" → triggerPlanCheck nhận option force.
        "triggerPlanCheck(jobId, { force: true })",
    ]
    for n in needles:
        if n not in src:
            print(f"[FAIL] TC-08 frontend :: thiếu {n!r}", flush=True)
            return 1
    # C2 regression: nhánh cd.expired KHÔNG được gọi triggerPlanCheck trực tiếp
    # nữa (sẽ flood mỗi giây) — chỉ được qua startPlanPoll.
    if "triggerPlanCheck(row.dataset.id)" in src:
        print("[FAIL] TC-08 frontend :: cd.expired vẫn gọi triggerPlanCheck trực tiếp (flood — C2)",
              flush=True)
        return 1
    # Regression guard: triggerPlanCheck KHÔNG được dùng obj-access vào Map.
    # Phạm vi grep gọn quanh function thân để tránh false-positive với
    # `state.jobs[id]` trong code khác (nếu có).
    fn_start = src.find("function triggerPlanCheck(")
    fn_end = src.find("\n  }\n", fn_start)
    if fn_start < 0 or fn_end < 0:
        print("[FAIL] TC-08 frontend :: không định vị được triggerPlanCheck", flush=True)
        return 1
    fn_body = src[fn_start:fn_end]
    if "state.jobs[" in fn_body:
        print("[FAIL] TC-08 frontend :: triggerPlanCheck dùng state.jobs[...] (sai, Map cần .get)",
              flush=True)
        return 1
    # H1 cleanup: applyRemove phải dừng poller (tránh timer leak sau remove).
    rm_start = src.find("function applyRemove(")
    rm_end = src.find("\n  }\n", rm_start)
    if rm_start < 0 or rm_end < 0 or "_stopPlanPoll(" not in src[rm_start:rm_end]:
        print("[FAIL] TC-08 frontend :: applyRemove thiếu _stopPlanPoll cleanup (leak — H1)", flush=True)
        return 1
    print("[PASS] TC-08 frontend :: render + poll + recheck + cleanup + Map access đúng", flush=True)
    return 0


def t09_css_classes() -> int:
    src = (ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    needles = [
        ".upi-plan-badge",
        ".upi-plan-badge.upi-plan-plus",
        ".upi-plan-badge.upi-plan-free",
        ".upi-plan-badge.upi-plan-err",
    ]
    for n in needles:
        if n not in src:
            print(f"[FAIL] TC-09 css :: thiếu selector {n!r}", flush=True)
            return 1
    print("[PASS] TC-09 css :: 4 selector OK", flush=True)
    return 0


def t10_parse_entitlement() -> int:
    from gpt_signup_hybrid_new.session_phase import _parse_entitlement_plan

    def ent(plan, active, expires=None):
        return {"accounts": {"default": {"entitlement": {
            "subscription_plan": plan,
            "has_active_subscription": active,
            "expires_at": expires,
        }}}}

    # (data, expected_plan, expected_is_plus)
    cases = [
        (ent("chatgptplusplan", True, "2026-07-17"), "plus", True),
        (ent("chatgptfreeplan", False), "free", False),
        # Strict Plus-only: Pro/Team active KHÔNG là Plus.
        (ent("chatgptproplan", True), "pro", False),
        (ent("chatgptteamplan", True), "team", False),
        # active=True nhưng plan free → false-positive guard.
        (ent("chatgptfreeplan", True), "free", False),
        # Plus label nhưng subscription hết hạn → is_plus False.
        (ent("chatgptplusplan", False), "plus", False),
        # Shape thiếu → None/False, không raise.
        ({}, None, False),
        ({"accounts": {}}, None, False),
        ({"accounts": {"default": {}}}, None, False),
        (None, None, False),
        # Không có "default" → lấy account đầu tiên.
        ({"accounts": {"acc-x": {"entitlement": {
            "subscription_plan": "chatgptplusplan", "has_active_subscription": True}}}}, "plus", True),
    ]
    for data, exp_plan, exp_plus in cases:
        got = _parse_entitlement_plan(data)
        if got.get("plan") != exp_plan or got.get("is_plus") != exp_plus:
            print(f"[FAIL] TC-10 parse :: {data!r} → plan={got.get('plan')!r} "
                  f"is_plus={got.get('is_plus')!r}, want plan={exp_plan!r} is_plus={exp_plus!r}",
                  flush=True)
            return 1
    # Mọi shape thiếu phải có đủ 4 key (không raise / không KeyError downstream).
    keys = set(_parse_entitlement_plan({}).keys())
    if keys != {"plan", "is_plus", "has_active_subscription", "expires"}:
        print(f"[FAIL] TC-10 parse :: blank shape sai key {keys}", flush=True)
        return 1
    print(f"[PASS] TC-10 parse :: {len(cases)} case OK (strict Plus-only)", flush=True)
    return 0


def t11_fetch_entitlement_signature() -> int:
    from gpt_signup_hybrid_new.session_phase import fetch_account_entitlement

    if not asyncio.iscoroutinefunction(fetch_account_entitlement):
        print("[FAIL] TC-11 fetch_entitlement :: phải là async", flush=True)
        return 1
    sig = inspect.signature(fetch_account_entitlement)
    if "access_token" not in sig.parameters:
        print(f"[FAIL] TC-11 fetch_entitlement :: thiếu param access_token {list(sig.parameters)}",
              flush=True)
        return 1
    print("[PASS] TC-11 fetch_entitlement :: async + có access_token", flush=True)
    return 0


def main() -> int:
    print("=== check_upi_plan_check ===", flush=True)
    tests = [
        t01_syntax,
        t02_upiqrresult_fields,
        t03_upijob_fields,
        t04_to_dict_shape,
        t05_check_plan_method,
        t06_extract_plan,
        t07_endpoint_registered,
        t08_frontend_hooks,
        t09_css_classes,
        t10_parse_entitlement,
        t11_fetch_entitlement_signature,
    ]
    fails = 0
    for t in tests:
        try:
            rc = t()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {t.__name__} :: exception {exc!r}", flush=True)
            rc = 1
        if rc:
            fails += 1
    print(f"=== done :: {len(tests) - fails}/{len(tests)} pass ===", flush=True)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
