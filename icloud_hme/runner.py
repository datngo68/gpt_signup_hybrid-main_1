"""HmeRunner — infinite-loop controller cho iCloud HME service layer.

State sau task 1.4:
    - Type alias ``LogCallback``: async callable ``(level, message, payload) -> None``.
    - Dataclass ``RunnerStats``: tổng hợp ``created/errors/skipped`` cộng dồn theo session.
    - Class ``HmeRunner``:
        * Constructor + 6 read-only property — task 1.2.
        * Lifecycle ``start``/``stop``/``pause``/``resume`` + helper
          ``_interruptible_sleep`` — task 1.3.
        * ``_run_one_cycle`` dispatch sang service layer (7 action whitelist) — task 1.4.

Runner KHÔNG chứa business logic; mọi work delegate xuống service layer
(``HmeGenerator``, ``ProfileChecker``, ``HmeManager``, ``IcloudPoolManager``).

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2,
3.3, 3.4, 4.1, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7,
10.1, 10.2.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

# Service layer + Settings chỉ dùng cho type hint trong constructor signature.
# Không cần load runtime → tránh cascade trigger ``icloud_hme/__init__.py``
# (vẫn đang dở dang ở các task 2.x xoá Job layer). ``from __future__ import
# annotations`` ở trên đảm bảo annotation đánh giá lazy (string), nên không
# cần resolve symbol thực ở module load time.
if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from config import Settings
    from .checker import ProfileChecker
    from .generator import HmeGenerator
    from .manager import HmeManager
    from .pool import IcloudPoolManager


# ── Type alias ───────────────────────────────────────────────────────────────
# Async callback signature (R10.1): ``(level, message, payload) -> Awaitable[None]``.
# Transport-agnostic: CLI ghi stderr, Web push SSE qua LogBuffer.
LogCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


# ── Stats ────────────────────────────────────────────────────────────────────
@dataclass
class RunnerStats:
    """Tổng hợp stats trong toàn session, đơn điệu không giảm (R5.1).

    Cộng dồn qua các cycle (R5.2):
        - ``created``  ← ``cycle_result["created"]``
        - ``errors``   ← ``len(cycle_result["failures"])``
        - ``skipped``  ← ``len(cycle_result["disabled_profiles"])``
    """

    created: int = 0
    errors: int = 0
    skipped: int = 0


# ── Per-profile runtime state (per-profile-cycle) ───────────────────────────
# Khi action='generate', Runner rotate tuần tự qua từng profile eligible —
# mỗi profile = 1 unit cycle riêng. UI cần biết:
#   - profile nào đang chạy (running)
#   - profile nào đang chờ tới lượt (waiting)
#   - profile nào đã xong lượt cycle này, đang chờ retry_interval (done)
#   - profile nào bị skip vì cooldown (limited/quota_full chưa đến hạn)
#   - profile nào terminal (session_expired/disabled/deleted)
# Cờ trạng thái này chỉ phục vụ observability; KHÔNG drive logic pick.
PROFILE_STATE_IDLE = "idle"          # chưa có lượt nào trong cycle hiện tại
PROFILE_STATE_RUNNING = "running"    # đang được dùng tạo email
PROFILE_STATE_WAITING = "waiting"    # đã trong queue rotate, chờ tới lượt
PROFILE_STATE_DONE = "done"          # đã xong lượt cycle này
PROFILE_STATE_COOLDOWN = "cooldown"  # limited/quota_full, chờ recover
PROFILE_STATE_DISABLED = "disabled"  # session_expired/disabled/deleted

# Default khoảng nghỉ giữa 2 profile trong cùng 1 cycle.
# Dùng để giảm tải Apple-side (tránh đập liên tiếp 5 IP/UA khác nhau cùng giây)
# + cho UI có thời gian render badge transition. KHÔNG ảnh hưởng quota logic.
_DEFAULT_INTRA_CYCLE_PAUSE_SEC: float = 2.0


# ── Runner ───────────────────────────────────────────────────────────────────
class HmeRunner:
    """Infinite-loop runner thay thế Job layer (~250 LOC khi hoàn chỉnh).

    Lifecycle (task 1.3 — implemented):
        - ``start(action, params)``: block tới khi cancel → return summary dict.
        - ``stop()``: set cancel_event; nếu pause đang set thì cũng set resume
          để đánh thức blocking await (R1.5).
        - ``pause()`` / ``resume()``: tạm dừng giữa các unit work.
        - Properties read-only: ``is_running``, ``current_action``, ``cycle_count``,
          ``stats``, ``retry_interval``, ``next_cycle_at``.
    """

    def __init__(
        self,
        *,
        generator: HmeGenerator,
        checker: ProfileChecker,
        hme_manager: HmeManager,
        pool_manager: IcloudPoolManager,
        settings: Settings,
        log_callback: LogCallback,
        retry_interval: Optional[int] = None,
    ) -> None:
        self._generator = generator
        self._checker = checker
        self._hme_manager = hme_manager
        self._pool_mgr = pool_manager
        self._settings = settings
        self._log_cb = log_callback
        # Override > settings default; Settings field do task 1.1 thêm vào.
        self._retry_interval: int = (
            retry_interval if retry_interval is not None else settings.icloud_retry_interval
        )

        # ── Runtime state (init khi start, reset cuối session) ──────────────
        self._cancel_event: Optional[asyncio.Event] = None
        self._pause_event: Optional[asyncio.Event] = None
        self._resume_event: Optional[asyncio.Event] = None
        self._running: bool = False
        self._current_action: Optional[str] = None
        self._cycle_count: int = 0
        self._next_cycle_at: Optional[float] = None  # epoch seconds, None khi không sleep
        self._stats: RunnerStats = RunnerStats()

        # ── Per-profile-cycle observability (action='generate' only) ───────
        # Map apple_id → state literal (xem PROFILE_STATE_*). Reset đầu cycle.
        self._profile_states: dict[str, str] = {}
        # Profile đang chạy ngay lúc này (None khi giữa 2 profile / sleep / idle).
        self._current_apple_id: Optional[str] = None
        # Pause giữa 2 profile trong cùng cycle (R6.1 mở rộng — per-profile rotate).
        self._intra_cycle_pause_sec: float = _DEFAULT_INTRA_CYCLE_PAUSE_SEC

    # ── Public read-only properties ─────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_action(self) -> Optional[str]:
        return self._current_action

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def stats(self) -> RunnerStats:
        return self._stats

    @property
    def retry_interval(self) -> int:
        return self._retry_interval

    @property
    def next_cycle_at(self) -> Optional[float]:
        return self._next_cycle_at

    @property
    def current_apple_id(self) -> Optional[str]:
        """Profile đang chạy NGAY lúc này (None khi giữa 2 profile / sleep)."""
        return self._current_apple_id

    @property
    def profile_states(self) -> dict[str, str]:
        """Snapshot map apple_id → state literal cho UI observability.

        Trả copy để caller không mutate state nội bộ. Empty dict khi
        Runner idle hoặc action != 'generate'.
        """
        return dict(self._profile_states)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    async def start(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Chạy infinite loop tới khi cancel; trả summary dict.

        Preconditions:
            - ``is_running == False`` — nếu trùng instance raise ``RuntimeError``
              **trước** khi reset state (R4.1).
            - ``params`` hợp lệ với ``action``.

        Postconditions:
            - ``is_running == False``, ``current_action is None``,
              ``next_cycle_at is None`` sau ``finally`` (R1.6).
            - Trả summary ``{total_cycles, created, errors, skipped, stopped_by}``
              khi cancel (R1.1).

        Loop invariants:
            - ``cycle_count`` đơn điệu không giảm, +1 mỗi cycle (R2.1).
            - ``stats.created/errors/skipped`` đơn điệu không giảm (R5.1).
        """
        # ── R4.1: Single-instance guard — raise TRƯỚC khi đụng state ────────
        if self._running:
            raise RuntimeError("Runner đang chạy action khác")

        # ── R4.3 + R5.3: Gán is_running + reset state TRƯỚC khi vào while ───
        self._running = True
        self._current_action = action
        self._cycle_count = 0
        self._stats = RunnerStats()
        self._cancel_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._next_cycle_at = None
        self._profile_states = {}
        self._current_apple_id = None

        try:
            await self._log_cb(
                "info", f"Runner started: {action}", {"action": action, "params": params}
            )

            while not self._cancel_event.is_set():
                # ── R2.1: tăng cycle_count đúng 1 khi bắt đầu cycle mới ─────
                self._cycle_count += 1
                self._next_cycle_at = None  # đang trong cycle, không trong sleep
                await self._log_cb(
                    "info",
                    f"── Cycle #{self._cycle_count} ──",
                    {"cycle": self._cycle_count},
                )

                cycle_result = await self._run_one_cycle(action, params)

                # ── R5.1 + R5.2: cộng dồn stats, đơn điệu không giảm ────────
                # Mapping per-action vì mỗi nhánh trong _run_one_cycle trả
                # shape khác (generate có "failures"+list, check_all có
                # "failed"+int, bulk có "failed"+list, list_sync không có
                # khái niệm error). Helper map về 3 key chuẩn.
                created_delta, errors_delta, skipped_delta = (
                    self._extract_stats_delta(action, cycle_result)
                )
                self._stats.created += created_delta
                self._stats.errors += errors_delta
                self._stats.skipped += skipped_delta

                # ── R2.2: log cycle done với message chứa số thứ tự + tóm tắt ─
                await self._log_cb(
                    "info",
                    f"Cycle #{self._cycle_count} done: {cycle_result}",
                    {"cycle": self._cycle_count, "result": cycle_result},
                )

                # Re-check cancel sau cycle để break ngay (R2.3 chỉ sleep khi
                # chưa cancel). Không dùng `else` vì loop condition chỉ check
                # đầu vòng.
                if self._cancel_event.is_set():
                    break

                # ── R2.3: chuyển sang sleep retry_interval giữa cycle ───────
                self._next_cycle_at = time.time() + self._retry_interval
                await self._log_cb(
                    "info",
                    f"Waiting {self._retry_interval}s before next cycle...",
                    {
                        "retry_interval": self._retry_interval,
                        "next_cycle_at": self._next_cycle_at,
                    },
                )
                interrupted = await self._interruptible_sleep(self._retry_interval)
                self._next_cycle_at = None
                if interrupted:
                    break

            # ── R1.1: trả summary dict đủ key khi cancel ────────────────────
            stopped_by = "user" if self._cancel_event.is_set() else "completed"
            summary: dict[str, Any] = {
                "total_cycles": self._cycle_count,
                "created": self._stats.created,
                "errors": self._stats.errors,
                "skipped": self._stats.skipped,
                "stopped_by": stopped_by,
            }
            await self._log_cb(
                "info", f"Runner stopped. Summary: {summary}", summary
            )
            return summary

        except Exception as exc:  # noqa: BLE001 — fan-out qua log rồi re-raise
            # R10.2: level "error" cho lỗi fatal.
            await self._log_cb(
                "error",
                f"Runner fatal error: {type(exc).__name__}: {exc}",
                {"error_type": type(exc).__name__},
            )
            raise
        finally:
            # ── R1.6: dù return summary hay raise, vẫn reset 3 field ────────
            self._running = False
            self._current_action = None
            self._next_cycle_at = None
            self._current_apple_id = None
            self._profile_states = {}

    def stop(self) -> None:
        """Signal cancel — non-blocking. Loop dừng tại checkpoint kế tiếp.

        - R1.2: set cancel_event mà không raise, không đụng cycle_count/stats.
        - R1.5: nếu pause_event đang set, set thêm resume_event để đánh thức
          ``await resume_event.wait()`` đang block trong ``_interruptible_sleep``.
        """
        if self._cancel_event is not None:
            self._cancel_event.set()
        if (
            self._pause_event is not None
            and self._resume_event is not None
            and self._pause_event.is_set()
        ):
            self._resume_event.set()

    def pause(self) -> None:
        """Pause — checkpoint kế tiếp sẽ block tới khi resume (R1.3)."""
        if self._pause_event is not None:
            self._pause_event.set()

    def resume(self) -> None:
        """Resume sau pause (R1.4)."""
        if self._resume_event is not None:
            self._resume_event.set()

    # ── Internal helpers ────────────────────────────────────────────────────
    @staticmethod
    def _extract_stats_delta(
        action: str, cycle_result: dict[str, Any]
    ) -> tuple[int, int, int]:
        """Map cycle_result của 1 action thành ``(created, errors, skipped)``.

        Mỗi nhánh trong ``_run_one_cycle`` trả shape khác — helper này tránh
        nhầm key giữa các action (R5.1 + R5.2):

            - ``generate``: ``created`` int + ``failures`` list +
              ``disabled_profiles`` list. created/errors/skipped đầy đủ.
            - ``check_all``: ``checked`` / ``ok`` / ``failed`` (int). Không
              tạo email mới, errors = ``failed``, skipped = 0.
            - ``deactivate_bulk`` / ``reactivate_bulk`` / ``delete_bulk`` /
              ``update_meta_bulk``: ``succeeded`` int + ``failed`` list.
              Không tạo email, errors = ``len(failed)``, skipped = 0.
            - ``list_sync``: chỉ có insert counts (active/inactive/unchanged),
              không có khái niệm error/skipped → 0/0/0.

        Action lạ (đã raise ValueError ở ``_run_one_cycle`` trước đó nên
        không tới đây) — fallback an toàn về (0, 0, 0).
        """
        if action == "generate":
            failures = cycle_result.get("failures", [])
            disabled = cycle_result.get("disabled_profiles", [])
            return (
                int(cycle_result.get("created", 0)),
                len(failures) if isinstance(failures, list) else 0,
                len(disabled) if isinstance(disabled, list) else 0,
            )
        if action == "check_all":
            return (0, int(cycle_result.get("failed", 0)), 0)
        if action in (
            "deactivate_bulk",
            "reactivate_bulk",
            "delete_bulk",
            "update_meta_bulk",
        ):
            failed = cycle_result.get("failed", [])
            errors = len(failed) if isinstance(failed, list) else int(failed or 0)
            return (0, errors, 0)
        if action == "list_sync":
            return (0, 0, 0)
        # Fallback — _run_one_cycle đã raise ValueError trước nhánh này, để
        # đây phòng test gọi trực tiếp _extract_stats_delta với action lạ.
        return (0, 0, 0)

    async def _run_one_cycle(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch sang service layer cho 1 cycle bounded (R6.1–6.7).

        Switch theo ``action`` rồi gọi đúng public method của service layer.
        Runner forward 3 event (cancel/pause/resume) xuống generator để work
        nội bộ cũng interruptible (R6.1). Mỗi nhánh map kết quả service về
        ``dict`` thuần để caller (``start``) cộng dồn ``stats``.

        KHÔNG sửa generator/checker/hme_manager — chỉ gọi public method (R6.7).

        Note (deviation R6.2): ``ProfileChecker.check_all`` hiện không nhận
        ``cancellation_event``; vẫn giữ KHÔNG sửa checker theo ràng buộc task
        1.4. Do đó ``check_all`` của 1 cycle chạy tới hết list eligible
        profile mới respect cancel ở vòng outer (giữa các cycle).

        Args:
            action: 1 trong 7 giá trị whitelist (R6.6).
            params: dict tham số tự do; mỗi nhánh tự đọc key tương ứng.

        Returns:
            dict kết quả cycle với key tuỳ action — caller chỉ đọc
            ``created`` / ``failures`` / ``disabled_profiles`` cho stats
            (xem ``start``); các key còn lại để log + UI hiển thị.

        Raises:
            ValueError: action không thuộc whitelist (R6.6).
            TypeError / KeyError: params thiếu field bắt buộc (e.g. list_sync
                cần ``apple_id``) — fail-fast lên caller.
        """
        if action == "generate":
            # Per-profile-cycle (icloud-runner-loop revised):
            # Thay vì gọi generator.generate() để nó round-robin tự pick + drain
            # toàn pool trong 1 lần, Runner snapshot list profile đầu cycle rồi
            # rotate tuần tự từng profile. Mỗi profile = 1 unit cycle riêng,
            # giúp UI hiển thị state running/waiting/done/cooldown rõ ràng và
            # giảm rủi ro 1 profile chiếm sóng vô hạn.
            #
            # Logic:
            # - Snapshot pool_repo.list_all() đầu cycle (tránh race với
            #   add_profile concurrent).
            # - Phân loại 2 set: ELIGIBLE (active / limited+expired /
            #   quota_full+expired) và COOLDOWN (limited / quota_full chưa
            #   đến hạn) và DISABLED (session_expired / disabled / deleted).
            # - Reset profile_states đầu cycle: ELIGIBLE → waiting,
            #   COOLDOWN → cooldown, DISABLED → disabled.
            # - For mỗi apple_id ELIGIBLE:
            #     * profile_states[apple_id] = running
            #     * generator.generate(target_apple_id=..., count=count_per_profile,
            #                          infinite=False)
            #     * cộng dồn stats + log per-profile.
            #     * profile_states[apple_id] = done
            #     * sleep _intra_cycle_pause_sec (interruptible) trước profile kế.
            # - Hết list → return aggregate cycle_result.
            #
            # ``count_per_profile`` đọc từ params: mặc định None = drain
            # bounded cho profile đó (tới khi hit limited/quota_full/auth).
            # User set int >=1 → mỗi profile tạo tối đa N email rồi nhường
            # profile kế. Mode mới này giúp rotate "fair" hơn — profile đầu
            # không chiếm hết quota cycle.
            count_per_profile = params.get("count_per_profile")
            if count_per_profile is None:
                # Backward-compat: nếu user vẫn truyền count_per_cycle (param
                # cũ), coi như per-profile (drain mỗi profile riêng). Trước
                # đây count_per_cycle áp lên cả pool — semantic đã đổi.
                count_per_profile = params.get("count_per_cycle")
            return await self._run_generate_per_profile(
                count_per_profile=count_per_profile,
                label=params.get("label"),
                note=params.get("note"),
                proxy=params.get("proxy"),
            )

        if action == "check_all":
            # R6.2: KHÔNG truyền cancellation_event vì checker chưa hỗ trợ
            # (xem note ở docstring). auto_mark default True theo design.
            results = await self._checker.check_all(
                auto_mark=params.get("auto_mark", True),
                proxy=params.get("proxy"),
            )
            return {
                "checked": len(results),
                "ok": sum(1 for r in results if r.ok),
                "failed": sum(1 for r in results if not r.ok),
            }

        if action in ("deactivate_bulk", "reactivate_bulk", "delete_bulk"):
            # R6.3: dynamic dispatch — 3 method cùng signature trên HmeManager.
            method = getattr(self._hme_manager, action)
            result = await method(
                params.get("emails", []),
                dry_run=params.get("dry_run", False),
            )
            return {
                "succeeded": result.succeeded,
                "failed": list(result.failed),
            }

        if action == "update_meta_bulk":
            # R6.4: items thay vì emails — schema riêng do design R9.
            result = await self._hme_manager.update_meta_bulk(
                params.get("items", []),
                dry_run=params.get("dry_run", False),
            )
            return {
                "succeeded": result.succeeded,
                "failed": list(result.failed),
            }

        if action == "list_sync":
            # R6.5: ``apple_id`` bắt buộc — KeyError fail-fast nếu thiếu.
            diff = await self._hme_manager.list_sync(params["apple_id"])
            return {
                "inserted_active": diff.inserted_active,
                "inserted_inactive": diff.inserted_inactive,
                "unchanged": diff.unchanged,
            }

        # R6.6: action ngoài 7 whitelist → ValueError.
        raise ValueError(f"Unknown action: {action}")

    # ── Per-profile generate cycle (action='generate' rotate flow) ─────────
    async def _run_generate_per_profile(
        self,
        *,
        count_per_profile: int | None,
        label: str | None,
        note: str | None,
        proxy: str | None,
    ) -> dict[str, Any]:
        """Rotate qua từng profile eligible 1 lần / cycle.

        Mỗi profile được pin qua ``generator.generate(target_apple_id=...)``
        để cô lập (không round-robin lẫn lộn); state map cập nhật để UI
        render badge running/waiting/done/cooldown/disabled real-time.

        Returns:
            cycle_result dict cùng shape generator output cũ:
            ``{"created", "requested", "failures", "disabled_profiles",
               "per_profile": [...]}`` — caller (start) chỉ đọc 3 key đầu
            cho stats; ``per_profile`` cho log + UI debug.

        Cancel:
            Cancel/pause check sau MỖI profile (không cần break giữa chừng
            vì pin generator đã forward event).
        """
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        # Tránh cycle import: dùng public method generator → pool_repo.list_all
        # gián tiếp là điều ta cần — nhưng generator không expose list_all.
        # Pool_repo nằm trong generator._pool_repo (private). Để KHÔNG sửa
        # generator API, dùng pool_manager (đã DI) → pool_manager._pool_repo
        # cũng private. Cleanest: snapshot qua hme_manager? Không có.
        # → Đọc list_all qua thuộc tính nội của generator (acceptable trong
        # cùng package; đã document rằng Runner & Generator coupled).
        pool_repo = self._generator._pool_repo  # noqa: SLF001 — same-package coupling
        all_accounts = pool_repo.list_all()
        now = _dt.now(_tz.utc).replace(tzinfo=None)

        eligible_ids: list[str] = []
        states: dict[str, str] = {}
        for acc in all_accounts:
            status = acc.status
            if status in ("session_expired", "disabled", "deleted"):
                states[acc.apple_id] = PROFILE_STATE_DISABLED
                continue
            if status == "active":
                eligible_ids.append(acc.apple_id)
                states[acc.apple_id] = PROFILE_STATE_WAITING
                continue
            if status == "limited":
                if acc.limited_until is not None and acc.limited_until <= now:
                    eligible_ids.append(acc.apple_id)
                    states[acc.apple_id] = PROFILE_STATE_WAITING
                else:
                    states[acc.apple_id] = PROFILE_STATE_COOLDOWN
                continue
            if status == "quota_full":
                if (
                    acc.quota_retry_until is not None
                    and acc.quota_retry_until <= now
                    and acc.hme_count < self._generator._hme_quota_limit  # noqa: SLF001
                ):
                    eligible_ids.append(acc.apple_id)
                    states[acc.apple_id] = PROFILE_STATE_WAITING
                else:
                    states[acc.apple_id] = PROFILE_STATE_COOLDOWN
                continue
            # Status không lường trước → coi như disabled (fail-safe).
            states[acc.apple_id] = PROFILE_STATE_DISABLED

        # Publish snapshot ngay đầu cycle để UI thấy bố cục.
        self._profile_states = states
        self._current_apple_id = None

        # Aggregate counters per cycle.
        total_created = 0
        total_requested = 0
        all_failures: list[dict[str, Any]] = []
        all_disabled: list[str] = []
        per_profile_summary: list[dict[str, Any]] = []

        for apple_id in eligible_ids:
            # Cancel check trước mỗi profile.
            if self._cancel_event is not None and self._cancel_event.is_set():
                break

            # Mark running.
            self._current_apple_id = apple_id
            self._profile_states[apple_id] = PROFILE_STATE_RUNNING
            await self._log_cb(
                "info",
                f"▶ Profile {apple_id} bắt đầu lượt cycle #{self._cycle_count}",
                {
                    "apple_id": apple_id,
                    "cycle": self._cycle_count,
                    "phase": "profile_start",
                },
            )

            try:
                profile_result = await self._generator.generate(
                    count=count_per_profile,
                    infinite=False,
                    label=label,
                    note=note,
                    proxy=proxy,
                    cancellation_event=self._cancel_event,
                    pause_event=self._pause_event,
                    resume_event=self._resume_event,
                    target_apple_id=apple_id,
                )
            except Exception as exc:  # noqa: BLE001
                # Generator raise → không phá cycle, log + skip profile.
                await self._log_cb(
                    "error",
                    f"✗ Profile {apple_id} lỗi: {type(exc).__name__}: {exc}",
                    {
                        "apple_id": apple_id,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                all_failures.append(
                    {
                        "apple_id": apple_id,
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                self._profile_states[apple_id] = PROFILE_STATE_DISABLED
                self._current_apple_id = None
                continue

            # Cộng dồn aggregate.
            total_created += profile_result.created
            total_requested += profile_result.requested
            for f in profile_result.failures:
                all_failures.append(
                    {
                        "apple_id": f.apple_id,
                        "error_class": f.error_class,
                        "error": f.error,
                    }
                )
            for d in profile_result.disabled_profiles:
                if d not in all_disabled:
                    all_disabled.append(d)
            per_profile_summary.append(
                {
                    "apple_id": apple_id,
                    "created": profile_result.created,
                    "failures": len(profile_result.failures),
                    "disabled": list(profile_result.disabled_profiles),
                }
            )

            # Mark done. Nếu profile bị disabled trong lượt (limited /
            # quota_full / session_expired vừa xảy ra) → state phản ánh
            # cooldown/disabled thay vì done.
            if apple_id in profile_result.disabled_profiles:
                # Re-read DB-status để biết profile đang ở cooldown hay disabled.
                updated = pool_repo.get(apple_id)
                if updated is None or updated.status in (
                    "session_expired", "disabled", "deleted"
                ):
                    self._profile_states[apple_id] = PROFILE_STATE_DISABLED
                else:
                    self._profile_states[apple_id] = PROFILE_STATE_COOLDOWN
            else:
                self._profile_states[apple_id] = PROFILE_STATE_DONE

            await self._log_cb(
                "info",
                (
                    f"■ Profile {apple_id} xong lượt: created="
                    f"{profile_result.created} fail={len(profile_result.failures)}"
                ),
                {
                    "apple_id": apple_id,
                    "created": profile_result.created,
                    "failures": len(profile_result.failures),
                    "phase": "profile_end",
                },
            )

            self._current_apple_id = None

            # Pause giữa 2 profile (interruptible). Bỏ pause cho profile cuối.
            if (
                apple_id != eligible_ids[-1]
                and self._intra_cycle_pause_sec > 0
                and (
                    self._cancel_event is None
                    or not self._cancel_event.is_set()
                )
            ):
                try:
                    await asyncio.sleep(self._intra_cycle_pause_sec)
                except asyncio.CancelledError:
                    raise

        self._current_apple_id = None

        return {
            "created": total_created,
            "requested": total_requested,
            "failures": all_failures,
            "disabled_profiles": all_disabled,
            "per_profile": per_profile_summary,
            "eligible_count": len(eligible_ids),
        }

    async def _interruptible_sleep(self, seconds: int) -> bool:
        """Sleep theo tick 1s, kiểm tra cancel/pause mỗi vòng.

        Args:
            seconds: số giây cần sleep (cast int).

        Returns:
            ``True`` nếu cancel_event được set trong lúc sleep (caller break loop).
            ``False`` nếu sleep đủ thời gian.

        Postcondition theo R3.x:
            - R3.1: cancel set tại t0 → return ``True`` trong ≤ 1.5s.
            - R3.2: kiểm tra cancel/pause đúng 1 lần mỗi 1 giây.
            - R3.3: pause set → log message chứa "Paused" và block bằng
              ``await resume_event.wait()``.
            - R3.4: resume set → clear cả resume_event và pause_event trước
              khi tiếp tục đếm thời gian sleep còn lại.
        """
        # Bảo vệ: events phải được khởi tạo (start() đã set). Trường hợp gọi
        # trực tiếp _interruptible_sleep từ test: events vẫn từ start() chuẩn bị.
        cancel_event = self._cancel_event
        pause_event = self._pause_event
        resume_event = self._resume_event
        if cancel_event is None or pause_event is None or resume_event is None:
            # Fail-fast: invariant của start() — events luôn != None khi vào loop.
            raise RuntimeError(
                "_interruptible_sleep gọi khi events chưa init (cần start() trước)"
            )

        for _ in range(int(seconds)):
            # R3.2: tick mở đầu — check cancel trước, pause sau.
            if cancel_event.is_set():
                return True

            if pause_event.is_set():
                # R3.3: log "Paused" rồi block tới khi resume.
                await self._log_cb(
                    "info", "Paused. Waiting for resume...", {"event": "pause"}
                )
                await resume_event.wait()
                # R3.4: clear cả resume_event và pause_event trước khi tiếp tục.
                #
                # INVARIANT (asyncio single-thread):
                # ``resume_event.wait()`` return → đoạn code dưới (clear +
                # re-check cancel + log) chạy ATOMIC vì KHÔNG có ``await``
                # nào chen vào trước ``await asyncio.sleep(1.0)`` ở cuối
                # vòng for. Nghĩa là user gọi ``pause()`` lại trong khoảng
                # này KHÔNG thể xen vào (asyncio chỉ nhường control khi gặp
                # await). Đây là lý do 2 dòng ``clear()`` an toàn không cần
                # lock.
                #
                # NẾU sau này thêm ``await`` giữa ``wait()`` và ``clear()``
                # (vd. log async), invariant này GÃY → cần refactor sang 1
                # state enum (IDLE/RUNNING/PAUSED/STOPPING) thay vì 3 event.
                resume_event.clear()
                pause_event.clear()
                # Re-check cancel ngay sau resume — stop() có thể đã set cả 2.
                if cancel_event.is_set():
                    return True
                await self._log_cb("info", "Resumed.", {"event": "resume"})

            await asyncio.sleep(1.0)

        return False
