"""Human-like input helpers cho browser_phase + session_phase.

Anti-ban (journal 260625-1224 Task 2.2 + bug B2 + C1):
    Sentinel SDK của OpenAI build so-token (Session Observer) bằng cách
    track DOM events:
        - keydown / keyup timings + variance + pause
        - input event sequence (focus → type → blur)
        - mousemove paths trước click
        - pointerdown / pointerup pattern
        - scroll, resize, hover events nhẹ

    Code cũ dùng ``loc.type(text, delay=80)`` cố định 80ms, KHÔNG jitter,
    KHÔNG pause, KHÔNG mouse movement → so-token nghèo nàn → server flag.

    Module này cung cấp helper mô phỏng người dùng thật:
    - ``human_type``  — gõ với delay Gaussian + occasional pause
    - ``human_click`` — mousemove tới element rồi click
    - ``random_mouse_wander`` — di chuột vài lần ngẫu nhiên
    - ``dwell``       — async sleep với jitter

Caller pattern:
    ```python
    from _human_input import human_type, human_click, dwell

    await dwell(0.4, 0.8)  # đọc trang (tối đa 1s)
    await human_type(page.locator('input[name="password"]'), pw,
                     delay_min_ms=45, delay_max_ms=110)
    await dwell(0.3, 0.8)  # tab/move
    await human_click(page, 'button[type="submit"]')
    ```
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Defaults — đồng bộ với Settings Store ``reg.human_typing_delay_ms_min/max``
# ─────────────────────────────────────────────────────────────────────

# Giảm fake human action (yêu cầu user): gõ nhanh hơn, ít pause hơn — vẫn giữ
# jitter Gaussian để sentinel SDK thấy biến thiên (không phải delay cố định).
DEFAULT_DELAY_MIN_MS = 45
DEFAULT_DELAY_MAX_MS = 110
DEFAULT_PAUSE_PROBABILITY = 0.03  # 3% ký tự sẽ pause ngắn sau khi gõ
DEFAULT_PAUSE_MIN_S = 0.1
DEFAULT_PAUSE_MAX_S = 0.3


# ─────────────────────────────────────────────────────────────────────
# Internal — Gaussian delay sampling
# ─────────────────────────────────────────────────────────────────────


def _sample_delay_ms(min_ms: int, max_ms: int) -> int:
    """Sample delay theo phân phối Gaussian, clamp vào [min_ms, max_ms].

    Mean = (min+max)/2, stddev = (max-min)/4 → 95% sample nằm trong khoảng.
    Người thật gõ phím có distribution Gaussian (Fitts's law variant) — biến
    cố định 80ms → bot signature rõ rệt.
    """
    if max_ms <= min_ms:
        return max(1, int(min_ms))
    mean = (min_ms + max_ms) / 2
    stddev = max(1.0, (max_ms - min_ms) / 4)
    raw = random.gauss(mean, stddev)
    return max(min_ms, min(max_ms, int(raw)))


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


async def dwell(min_s: float = 0.2, max_s: float = 0.6) -> None:
    """Async sleep với jitter uniform [min_s, max_s], HARD CAP 1.0s.

    Dùng giữa các state transition để page settle + sentinel SDK observe
    'idle reading time'. Tránh bot pattern click-click liên tục.

    Hard cap 1s (yêu cầu user): mọi dwell không bao giờ kéo dài quá 1 giây —
    dù caller truyền max_s lớn hơn — để không làm chậm flow đăng ký.
    """
    lo = max(0.0, min(min_s, 1.0))
    hi = max(lo, min(max_s, 1.0))
    await asyncio.sleep(random.uniform(lo, hi))


async def human_type(
    locator: Any,
    text: str,
    *,
    delay_min_ms: int = DEFAULT_DELAY_MIN_MS,
    delay_max_ms: int = DEFAULT_DELAY_MAX_MS,
    pause_probability: float = DEFAULT_PAUSE_PROBABILITY,
    pause_min_s: float = DEFAULT_PAUSE_MIN_S,
    pause_max_s: float = DEFAULT_PAUSE_MAX_S,
    clear_before: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Gõ ``text`` vào ``locator`` mô phỏng người dùng thật.

    Strategy:
        1. Click locator (force=True để focus dù bị overlay ẩn).
        2. Clear value cũ (fill("")) — nếu ``clear_before=True``.
        3. Loop từng ký tự:
           a. Sample delay Gaussian [delay_min_ms, delay_max_ms].
           b. ``locator.type(ch, delay=delay_ms)``.
           c. Với xác suất ``pause_probability``, sleep [pause_min_s, pause_max_s]
              (mô phỏng "đọc/think").

    Sentinel SDK quan sát ``input``/``keydown`` events sẽ thấy variance
    realistic + occasional long pause = human-like distribution.

    Args:
        locator: Playwright/Camoufox locator (page.locator('input[...]').first).
        text: chuỗi cần gõ.
        delay_min_ms / delay_max_ms: dải delay per-key (mặc định 120-260).
        pause_probability: xác suất pause sau mỗi ký tự (mặc định 8%).
        pause_min_s / pause_max_s: khoảng pause (giây).
        clear_before: True = fill("") trước khi gõ.
        log: optional callable để log progress.
    """
    _log = log or (lambda m: logger.debug(m))

    try:
        await locator.click(force=True, timeout=3000)
    except Exception as exc:  # noqa: BLE001 — best-effort
        _log(f"[human_type] click before type failed (continue): {exc}")

    if clear_before:
        try:
            await locator.fill("")
        except Exception as exc:  # noqa: BLE001
            _log(f"[human_type] fill('') before type failed (continue): {exc}")

    # Small pre-type pause — người thật click rồi suy nghĩ (giảm cho nhanh)
    await asyncio.sleep(random.uniform(0.02, 0.08))

    # PERF: gõ qua ``page.keyboard`` (focus 1 lần) thay vì ``locator.type(ch)``
    # mỗi ký tự. ``locator.type`` chạy actionability/stability check cho element
    # MỖI lần gọi — trên trang nặng (sentinel SDK + Turnstile mutate DOM liên
    # tục) mỗi check có thể chờ vài giây → gõ 12 ký tự mất 20-30s. Keyboard-level
    # type chỉ check 1 lần (lúc focus) rồi bắn keydown/keyup trực tiếp vào element
    # đang focus → nhanh, vẫn đủ event cho sentinel observer.
    #
    # ``locator.page`` là property của Playwright Locator (bản mới). Nếu không có
    # (locator giả trong test / Playwright cũ) → fallback ``locator.type`` giữ
    # nguyên hành vi cũ (không regression).
    page = getattr(locator, "page", None)
    if page is not None:
        try:
            await locator.focus(timeout=3000)
        except Exception as exc:  # noqa: BLE001
            _log(f"[human_type] focus before keyboard type failed (continue): {exc}")

    for idx, ch in enumerate(text):
        delay_ms = _sample_delay_ms(delay_min_ms, delay_max_ms)
        try:
            if page is not None:
                await page.keyboard.type(ch, delay=delay_ms)
            else:
                await locator.type(ch, delay=delay_ms)
        except Exception as exc:  # noqa: BLE001
            _log(f"[human_type] type idx={idx} char={ch!r} failed: {exc}")
            raise
        # Occasional human pause (think/look at screen)
        if random.random() < pause_probability:
            await asyncio.sleep(random.uniform(pause_min_s, pause_max_s))

    # Post-type settle (giảm cho nhanh)
    await asyncio.sleep(random.uniform(0.03, 0.10))


async def human_click(
    page: Any,
    target: Any,
    *,
    timeout_ms: int = 3000,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Click ``target`` — để Camoufox humanize tự animate cursor tới element.

    TRƯỚC đây hàm này tự ``page.mouse.move(steps=N)`` rồi mới click → THÀNH 2
    lần di chuột (move thủ công + move tự động của click), mỗi lần Camoufox
    humanize animate tới ~1.5s → "di chuột rất lâu". Bỏ explicit move: chỉ gọi
    ``locator.click`` — Camoufox humanize (đã cap ~0.4s ở launch) tự sinh
    mousemove path realistic cho sentinel observer. Nhanh hơn nhiều.

    Args:
        page: Playwright Page (giữ để tương thích chữ ký caller).
        target: selector string HOẶC Locator object.
        timeout_ms: timeout cho click.
    """
    _log = log or (lambda m: logger.debug(m))

    if isinstance(target, str):
        locator = page.locator(target).first
    else:
        locator = target  # đã là locator

    # Click trực tiếp — Camoufox humanize tự lo cursor movement. delay nhỏ cho
    # mousedown→mouseup giống người (không phải 0ms như bot).
    await locator.click(timeout=timeout_ms, delay=random.randint(10, 35))


# ─────────────────────────────────────────────────────────────────────
# /about-you birth field — year-of-birth vs age resolver
# ─────────────────────────────────────────────────────────────────────


def _to_int(value: Any) -> Optional[int]:
    """Parse str/number sang int, trả None nếu rỗng/không hợp lệ."""
    try:
        s = str(value).strip()
        return int(s) if s else None
    except (TypeError, ValueError):
        return None


def age_from_birthdate(birthdate: str, today: Optional[date] = None) -> int:
    """Tính tuổi tròn từ birthdate ISO ``YYYY-MM-DD`` (UI rule: year diff, đã
    qua sinh nhật năm nay chưa)."""
    today = today or datetime.utcnow().date()
    y, m, d = (int(x) for x in birthdate.split("-"))
    return today.year - y - ((today.month, today.day) < (m, d))


async def resolve_birth_field_value(
    locator: Any,
    birthdate: str,
    *,
    today: Optional[date] = None,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[str, str]:
    """Quyết định giá trị điền cho input number trên ``/about-you``.

    OpenAI A/B test field này giữa 2 dạng (cùng ``name="age"``):
      - **YEAR OF BIRTH**: ``min`` ≈ 1896, ``max`` ≈ (năm hiện tại − 13).
        Validation: "Enter a valid year of birth" → phải điền NĂM SINH (vd
        "1999"), KHÔNG phải tuổi.
      - **AGE (tuổi)**: ``min`` ≈ 1, ``max`` ≈ 100/120 → điền TUỔI (vd "27").

    Phân biệt bằng cách đọc ``min``/``max``/``name``/``placeholder`` thật của
    element (fail-fast, không hardcode năm). Trả ``(value_str, kind)`` với
    ``kind`` ∈ {"year", "age"}.

    Args:
        locator: Playwright Locator trỏ tới input number.
        birthdate: chuỗi ISO ``YYYY-MM-DD``.
        today: override ngày hôm nay (cho test).
        log: optional logger.
    """
    _log = log or (lambda m: logger.debug(m))

    parts = birthdate.split("-")
    if len(parts) != 3:
        raise ValueError(f"birthdate format sai (cần YYYY-MM-DD): {birthdate!r}")
    year = parts[0]
    age = age_from_birthdate(birthdate, today=today)

    meta: dict[str, Any] = {}
    try:
        meta = await locator.evaluate(
            "(el) => ({min: el.min || '', max: el.max || '', "
            "name: el.name || '', placeholder: el.placeholder || '', "
            "ariaLabel: el.getAttribute('aria-label') || ''})"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, fallback dưới
        _log(f"[birth] đọc min/max input thất bại ({exc}) — đoán theo birthdate")

    min_v = _to_int(meta.get("min"))
    max_v = _to_int(meta.get("max"))
    hints = " ".join(
        str(meta.get(k, "")) for k in ("name", "placeholder", "ariaLabel")
    ).lower()

    # OpenAI đặt ``name="age"`` cho cả 2 dạng field → KHÔNG tin được tên field.
    # Chỉ ``min``/``max`` dạng số mới phân biệt chắc chắn:
    #   - YEAR OF BIRTH: max ≈ 2013 (>=1900) hoặc min ≈ 1896 (>=1000).
    #   - AGE: max ≈ 100/120 (< 1900), hoặc chỉ có min nhỏ (< 1000).
    # Mặc định YEAR (UI /about-you hiện tại) khi không có ràng buộc số rõ ràng —
    # an toàn hơn vì điền tuổi vào field năm sinh là root cause của bug.
    is_year = True
    if max_v is not None and max_v < 1900:
        is_year = False
    elif max_v is None and min_v is not None and min_v < 1000:
        is_year = False
    # Nhãn nhắc tới năm sinh → ép year (override mọi suy đoán số phía trên).
    if "year" in hints or "birth" in hints or "dob" in hints:
        is_year = True
    if is_year:
        return year, "year"
    return str(age), "age"


# ─────────────────────────────────────────────────────────────────────


async def random_mouse_wander(
    page: Any,
    *,
    count: int = 2,
    settle_min_s: float = 0.1,
    settle_max_s: float = 0.4,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Di chuột ``count`` lần tới điểm random trong viewport.

    Sentinel observer record mousemove events → cho thấy "có cursor activity"
    giữa các action. Người thật KHÔNG ngồi yên — cursor luôn drift nhẹ.

    Best-effort: không raise nếu page chết hay viewport không lấy được.
    """
    _log = log or (lambda m: logger.debug(m))

    try:
        vw, vh = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
        vw = int(vw or 1280)
        vh = int(vh or 720)
    except Exception as exc:  # noqa: BLE001
        _log(f"[mouse_wander] viewport probe failed (skip): {exc}")
        return

    for _ in range(max(0, count)):
        x = random.randint(int(vw * 0.1), int(vw * 0.9))
        y = random.randint(int(vh * 0.1), int(vh * 0.9))
        steps = random.randint(8, 20)
        try:
            await page.mouse.move(x, y, steps=steps)
        except Exception as exc:  # noqa: BLE001
            _log(f"[mouse_wander] move failed (skip remaining): {exc}")
            return
        await asyncio.sleep(random.uniform(settle_min_s, settle_max_s))


__all__ = [
    "human_type",
    "human_click",
    "random_mouse_wander",
    "dwell",
    "resolve_birth_field_value",
    "DEFAULT_DELAY_MIN_MS",
    "DEFAULT_DELAY_MAX_MS",
]
