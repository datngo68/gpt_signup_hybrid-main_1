# UPI Live Entitlement Plan-Check Fix Complete

**Date**: 2026-06-17 14:10  
**Severity**: Medium  
**Component**: UPI badge refresh logic, plan-check phase  
**Status**: Resolved (pending manual web-UI verification)

## What Happened

Badge stayed FREE after UPI payment upgraded account to Plus. Root cause: two-layer lag:
1. `/api/auth/session` cache (`account.planType`) reflects stale token state
2. `triggerPlanCheck` ran once at QR expiry with cache-guard → locked FREE forever if upgrade hadn't propagated

Fixed by reading plan from **live backend** `/backend-api/accounts/check/v4-2023-04-27` (entitlement hits DB) with session fallback; killed one-shot guard; added auto-poll 20s×6 (~2min) + manual Recheck button.

## The Brutal Truth

This fix touches four distinct surfaces (backend http layer, job manager, frontend polling, test harness), each with its own failure mode. Worst part: **Phase 1 probe was the actual gate** — if live endpoint also lagged, all Phase 2/3 work was wasted. Thankfully probe passed (live reflects Plus immediately), but forced us to bake in decision-point skip logic (Phase 1 FAIL → ship Phase 4 only). 

Also: pre-baked Docker image means code changes don't take effect in live container until `docker compose build && up -d` — almost deployed untested changes twice.

## Technical Details

- **Phase 1 (verified prior)**: `/backend-api/accounts/check/v4-2023-04-27` live endpoint reflects Plus immediately (status 200, Bearer token + full header recipe: Origin, Referer, x-openai-target-path/-route, OAI-Language, UA, sec-ch-ua trio). Minimal headers → 403 Cloudflare. No-proxy works. Shape: `accounts.default.entitlement.{subscription_plan,has_active_subscription,expires_at}`.

- **Phase 2 (session_phase.py, +153 LOC)**: `_parse_entitlement_plan` extracts subscription shape; `fetch_account_entitlement` async (Bearer + backend-api recipe, **distinct from** `fetch_session_via_http` cookie recipe). M1 violation fixed: errors carry status code only (no body leaked); token scrubbed via `_scrub_jwt` regex `eyJ[A-Za-z0-9_\-]+` stripping header segment from logs broadcast to SSE clients.

- **Phase 3 (manager.py, ~120 LOC)**: `UpiJobManager.check_plan` now live-first → `except SessionError` fallback to session cache `_extract_plan_from_session`. Dict shape unchanged (single caller server.py:1723). `is_plus` strict Plus-only: `has_active_subscription AND label=="plus"` (Pro/Team/Enterprise→False). Expires kept session-expiry (not subscription, avoids breaking countdown `qr_expires_at`). Added field `_active_proxy` to route checks through job's proxy pool.

- **Phase 4 (upi.js, +86/-14 LOC)**: `triggerPlanCheck(jobId,{force})` returns Promise<bool>. Completion-driven auto-poll `startPlanPoll`/`_planPollTick`/`_stopPlanPoll` (20s × 6 real checks; count-only, no wall-clock cap — respects user-locked decision "6 coping checks"). Early-return guard `if (_planPollState.has(id))` prevents 1 req/s flood. Cd.expired branch calls `startPlanPoll` not `triggerPlanCheck` (C2 anti-flood). New Recheck button (force check). Poll cleanup wired into `applyRemove` + `applyJobUpdate` (H1 leak fix).

## What We Tried

1. **Session cache alone** → lagged & stale → required live endpoint
2. **One-shot check at QR expiry** → stuck FREE if upgrade delayed → killed guard, added polling
3. **Wall-clock cap on poll** → worst-case ~40s per check × 6 = ~4–6 min, not ~2 min promised → locked count-only (user decision)
4. **Minimize headers for live call** → 403 Cloudflare → baked full recipe from `upi_runner.py` (Bearer+Origin+Referer+x-openai-target-*+OAI-Language+UA+sec-ch-ua trio)
5. **Inline token in logs** → token leak surface → added `_scrub_jwt` scrubbing eyJ… from error messages

## Root Cause Analysis

**Layer 1 — Cache Lag**: Account upgrade event doesn't cascade instantly to `/api/auth/session` endpoint. Live entitlement DB query is single source of truth, but requires Bearer token + expensive-to-reverse header recipe.

**Layer 2 — Guard Stickiness**: `triggerPlanCheck` guarded by `if (job.plan_check) return` after first call → assuming state was stable. Wrong assumption: upgrade event is async. Needed completion-driven re-check loop instead.

**Layer 3 — Integration Blind Spot**: Docker image bakes application code at build-time; runtime code mounts over volume → changes don't propagate without rebuild. Tests run in ephemeral container, code runs in persistent image → easy to test-pass but runtime-fail.

## Lessons Learned

1. **Cache lag is endemic.** Always prove live endpoint holds newer data before investing in integration. Phase 1 probe wasn't optional — it was the gate. Should have been mandatory before cook Phase 2/3.

2. **Guard conditions are temporary fixes.** One-shot guards work until async events violate the assumption. If you add a guard, document its invariant and time-to-invalidation.

3. **Completion-driven beats interval-driven for unpredictable I/O.** `setTimeout` in `.then` avoided the setInterval-overlap trap when a single check takes 40s (H4 intent baked correctly).

4. **Docker: persist the source of truth.** Baked image + volume mount for code is a foot-gun. Tests run clean; production runs stale. Document this prominently.

5. **Token-scrub regex is load-bearing.** `_scrub_jwt` is stronger than the pre-existing `fetch_session_via_http` error path (which still leaks body). New code can hold a higher bar than legacy code — do so.

## Next Steps

1. **Manual web-UI verification** (user-dependent): rebuild image `docker compose build && up -d`, upgrade a real account mid-flow, verify badge FREE→PLUS flips within ~2 min. ⚠️ **Blocker for production deploy**.

2. **Monitor SSE log stream** during manual test: confirm `check_plan` fires 6 times + stops on Plus or count exhaustion; no token leak in browser console.

3. **Defer fallback improvements**: Cookie-auth branch of `fetch_account_entitlement` (`:817–819`) is unexercised (Bearer 200 always). Leave as YAGNI reserve (next Cloudflare 403 will force it).

4. **Consider alerting on Phase 1 gate skip**: If Phase 1 FAIL (live also lags), auto-ship Phase 4 only — log this decision loudly so support knows entitlement check fell back to session-cache polling.

---

**Verification Summary**:  
- Harness 11/11 PASS (static grep t10 parser, t11 signature, updated t08 frontend hooks)
- Behavioral test 4/4 PASS (`test_check_plan_live.py`: t12 live-wins-cache, t13 fallback, t14 no-cookies fail-soft, t15 strict-Plus)
- `node --check upi.js` OK
- Code review: 0 Critical/High bugs; all red-team findings (C1,C2,H1,H4,M1,M3,M5) verified in code
- Docker one-off container tests pass; baked image tests pending manual web UI
