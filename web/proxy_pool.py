"""ProxyPool — xoay vòng nhiều proxy line/template để tránh trùng IP khi chạy batch.

Pool lưu **raw line/template** (``host:port:user:pass`` có thể chứa ``{SID}``),
KHÔNG phải URL đã normalize. Mọi consumer lấy giá trị từ pool và feed cho
curl_cffi/httpx/browser **PHẢI** ``materialize_proxy`` trước (xem ``proxy_format``).
``pick``/``mark_dead`` dùng key = raw line.

Single source of truth runtime cho danh sách proxy rotation. Được hydrate từ
Settings Store (`proxy.pool` + `proxy.rotation_mode`) lúc startup
(`JobManager.apply_settings`) và reconfigure khi user lưu qua endpoint
`POST /api/proxy/pool`.

Thiết kế:
- Một singleton dùng CHUNG cho cả 3 manager (reg / session / link) để rotation
  là toàn cục → 2 job song song không pick cùng 1 IP liên tiếp.
- ``pick()`` chọn proxy kế tiếp trong số proxy còn "live" theo mode
  (round_robin | random). Proxy đã bị ``mark_dead`` sẽ bị loại khỏi vòng xoay.
- Pool rỗng hoặc tất cả proxy đã chết → ``pick()`` trả None; caller chạy direct
  (không proxy). Đây là nguồn cấu hình proxy DUY NHẤT của hệ thống.

Concurrency: FastAPI chạy single event loop, các method mutate state đồng bộ
(không ``await`` giữa chừng) nên không cần lock.
"""
from __future__ import annotations

import random
import threading

_VALID_MODES: tuple[str, ...] = ("round_robin", "random", "probe")


def normalize_proxies(proxies) -> list[str]:
    """Strip + bỏ rỗng + dedupe (giữ thứ tự). Bỏ qua phần tử không phải str."""
    if not proxies:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in proxies:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


class ProxyPool:
    """Danh sách proxy xoay vòng + theo dõi proxy chết.

    Attributes (read qua property):
        entries: list proxy URL đã cấu hình (đã normalize).
        mode: "round_robin" | "random".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[str] = []
        self._mode: str = "round_robin"
        self._dead: set[str] = set()
        self._cursor: int = 0
        # Proxy line đứng đầu (first_proxy) của job GẦN NHẤT đã phát qua
        # ``ordered_for_job``. Dùng cho ``random`` mode để đảm bảo job kế tiếp
        # KHÔNG nhận lại cùng first_proxy (no-immediate-repeat) — random thuần
        # cho phép trùng IP liên tiếp khiến user tưởng "kẹt 1 proxy".
        self._last_first: str | None = None
        # Lease count: số job ĐANG chạy giữ mỗi proxy (key=raw line). Dùng để
        # chọn first_proxy theo "least-used" → rải đều giữa các job song song
        # (≤N job với N proxy → mỗi job 1 IP riêng; vượt N → tái dùng IP ít
        # tải nhất). acquire() tăng, release() giảm; entry về 0 thì xoá khỏi
        # dict để introspection sạch.
        self._leases: dict[str, int] = {}

    # ── Config ──────────────────────────────────────────────────────────

    def configure(self, proxies, mode: str | None = None) -> None:
        """Cập nhật danh sách proxy + mode.

        - ``proxies=None`` → giữ nguyên danh sách hiện tại (chỉ đổi mode nếu có).
        - ``proxies=[...]`` → thay thế toàn bộ; dead-set giữ lại entry vẫn còn,
          loại entry đã bị gỡ.
        - ``mode`` không hợp lệ → giữ mode cũ.
        """
        with self._lock:
            if mode in _VALID_MODES:
                self._mode = mode
            if proxies is not None:
                normalized = normalize_proxies(proxies)
                present = set(normalized)
                self._entries = normalized
                # Giữ dead-mark cho proxy vẫn còn trong pool, xoá phần đã gỡ.
                self._dead = {d for d in self._dead if d in present}
                if self._cursor >= len(normalized):
                    self._cursor = 0
                # last_first không còn trong pool → reset để no-repeat không
                # so sánh với entry đã gỡ.
                if self._last_first is not None and self._last_first not in present:
                    self._last_first = None
                # Drop lease cho proxy đã gỡ khỏi pool (tránh dict phình + lệch
                # least-used view với entry không còn tồn tại).
                self._leases = {p: c for p, c in self._leases.items() if p in present}

    def reset_dead(self) -> None:
        """Xoá toàn bộ đánh dấu chết — cho mọi proxy cơ hội chạy lại."""
        with self._lock:
            self._dead.clear()

    # ── Runtime selection ───────────────────────────────────────────────

    def pick(self) -> str | None:
        """Trả proxy kế tiếp trong số proxy còn live, hoặc None nếu không có.

        round_robin: xoay tuần tự qua danh sách live.
        random: chọn ngẫu nhiên trong danh sách live.
        probe: cũng dùng round_robin order; caller (``acquire_live_proxy``)
            probe + SID-rotate trước khi cấp cho job (xem ``proxy_health``).
        """
        with self._lock:
            live = [p for p in self._entries if p not in self._dead]
            if not live:
                return None
            if self._mode == "random":
                return random.choice(live)
            url = live[self._cursor % len(live)]
            self._cursor += 1
            return url

    def acquire(self) -> str | None:
        """Lease 1 proxy live cho 1 job, chọn theo **least-used** để rải đều.

        Chiến lược (phương án "hạn chế trùng nhiều nhất có thể"):
          1. Lọc proxy live (loại dead).
          2. Chọn nhóm có lease count THẤP NHẤT (ít job đang dùng nhất).
          3. Trong nhóm đó, ưu tiên KHÁC first_proxy job liền trước
             (no-immediate-repeat), rồi random tie-break.
          4. Tăng lease cho proxy được chọn + nhớ ``_last_first``.

        → Khi số job song song ≤ số proxy live: mỗi job 1 IP riêng (lease=0
        cho mỗi proxy mới). Vượt quá: job thừa nhận proxy ít tải nhất, không
        chờ. Caller PHẢI gọi ``release()`` (finally) khi job kết thúc.

        Pool rỗng / hết live → ``None`` (caller chạy direct).
        """
        with self._lock:
            live = [p for p in self._entries if p not in self._dead]
            if not live:
                return None
            min_load = min(self._leases.get(p, 0) for p in live)
            candidates = [p for p in live if self._leases.get(p, 0) == min_load]
            if len(candidates) > 1 and self._last_first in candidates:
                filtered = [p for p in candidates if p != self._last_first]
                if filtered:
                    candidates = filtered
            choice = random.choice(candidates)
            self._leases[choice] = self._leases.get(choice, 0) + 1
            self._last_first = choice
            return choice

    def release(self, line: str | None) -> None:
        """Trả lease cho 1 proxy (gọi trong finally của job). Idempotent-safe."""
        if not line:
            return
        with self._lock:
            cur = self._leases.get(line, 0)
            if cur <= 1:
                self._leases.pop(line, None)
            else:
                self._leases[line] = cur - 1

    def ordered_for_job(self, first: str | None = None) -> list[str]:
        """Trả list live entries đã sắp xếp theo ``mode`` cho consumer kiểu batch.

        Khác ``live_entries()`` (giữ order cố định): method này HONOR mode để
        các consumer tiêu thụ cả pool theo batch (vd UPI approve loop) không
        bị lockstep cùng IP khi chạy nhiều job song song.

        - ``first`` (raw line, thường lấy từ ``acquire()``): nếu còn live → ghim
          làm phần tử [0] (= first_proxy login/checkout của job), phần còn lại
          xáo trộn theo sau. Giúp first_proxy bám đúng proxy đã lease
          (least-used) → distinct giữa các job song song. ``first`` không còn
          live → bỏ qua, rơi về order theo ``mode``.
        - ``random``: shuffle + **no-immediate-repeat** (first khác job trước).
        - ``round_robin`` / ``probe``: rotate theo cursor (advance 1 bước/job).

        Pool rỗng / hết live → ``[]`` (caller chạy direct).
        """
        with self._lock:
            live = [p for p in self._entries if p not in self._dead]
            if not live:
                return []
            # Ghim proxy đã lease lên đầu (path chính của UPI khi dùng acquire).
            if first is not None and first in live:
                rest = [p for p in live if p != first]
                random.shuffle(rest)
                self._last_first = first
                return [first] + rest
            if self._mode == "random":
                shuffled = live[:]
                random.shuffle(shuffled)
                # No-immediate-repeat: nếu còn >1 proxy mà first trùng job
                # trước → swap phần tử [0] với 1 phần tử khác (cũng ngẫu nhiên)
                # để IP đổi mà vẫn giữ tính random toàn cục.
                if len(shuffled) > 1 and shuffled[0] == self._last_first:
                    swap_idx = random.randint(1, len(shuffled) - 1)
                    shuffled[0], shuffled[swap_idx] = shuffled[swap_idx], shuffled[0]
                self._last_first = shuffled[0]
                return shuffled
            offset = self._cursor % len(live)
            self._cursor += 1
            self._last_first = live[offset]
            return live[offset:] + live[:offset]

    def mark_dead(self, url: str | None) -> bool:
        """Đánh dấu proxy chết → loại khỏi vòng xoay. True nếu vừa mới đánh dấu."""
        if not url:
            return False
        with self._lock:
            if url not in self._entries or url in self._dead:
                return False
            self._dead.add(url)
            return True

    def mark_alive(self, url: str | None) -> None:
        """Gỡ đánh dấu chết cho 1 proxy (vd sau khi test lại OK)."""
        if not url:
            return
        with self._lock:
            self._dead.discard(url)

    # ── Introspection ───────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def entries(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def live_entries(self) -> list[str]:
        with self._lock:
            return [p for p in self._entries if p not in self._dead]

    def is_active(self) -> bool:
        """True nếu pool có ít nhất 1 proxy live → override proxy đơn."""
        with self._lock:
            return any(p not in self._dead for p in self._entries)

    def status(self) -> dict:
        """Snapshot trạng thái pool cho UI (đếm + danh sách dead đã mask credential).

        Dead-list mask qua ``proxy_format.mask_proxy`` để KHÔNG lộ user:pass/SID-pass
        raw ra ``GET /api/proxy/pool`` (F-F).
        """
        from .proxy_format import mask_proxy
        with self._lock:
            live = [p for p in self._entries if p not in self._dead]
            return {
                "mode": self._mode,
                "total": len(self._entries),
                "live": len(live),
                "dead": sorted(mask_proxy(d) for d in self._dead),
            }


# Module-level singleton dùng chung cho cả 3 manager + endpoint.
_PROXY_POOL: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool:
    """Trả singleton ProxyPool (lazy-init)."""
    global _PROXY_POOL  # noqa: PLW0603
    if _PROXY_POOL is None:
        _PROXY_POOL = ProxyPool()
    return _PROXY_POOL


__all__ = ["ProxyPool", "get_proxy_pool", "normalize_proxies"]
