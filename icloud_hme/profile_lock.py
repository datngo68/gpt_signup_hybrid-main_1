"""Profile_Lock — RW lock per Apple_ID qua thư viện ``filelock``.

Refs:
    requirements.md R12.14, R12.15, R12.16
    design.md §15 (Profile_Lock), Property 29 (concurrent safety)
    tasks.md task 7 + 7.1

Pattern: counter-based reader-writer lock multi-process.

File layout trong ``lock_dir`` (caller pass thường là
``runtime/icloud_profiles/<apple_id>/.lock/``):

    icloud-<safe>.write.lock     FileLock exclusive cho writer
    icloud-<safe>.read.sentinel  FileLock cho counter critical section
    icloud-<safe>.read.count     File text chứa số reader đang giữ

Semantics:

* ``write_lock`` (Bootstrap_Flow R12.14, Recorder R12.16): block mọi writer
  khác qua ``writer.lock``, sau đó poll ``read.count == 0`` qua sentinel.
  Khi cả hai điều kiện đạt → yield. Writer giữ ``writer.lock`` trong suốt
  critical section → reader mới phải block.
* ``read_lock`` (Session_Extractor R12.15): poll ``writer.lock`` không bị
  giữ qua probe non-blocking, sau đó increment counter atomic qua
  sentinel. Multiple reader OK.

Race resolution: cả hai mode dùng chung sentinel khi đụng counter, nên
``writer poll counter`` và ``reader probe writer`` không bao giờ bypass
nhau. Writer-priority sau khi writer đã acquire ``writer.lock``.
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path
from typing import Generator

from filelock import FileLock, Timeout

from .exceptions import ProfileLockError

_POLL_INTERVAL_SEC = 0.05
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_apple_id_for_lock(apple_id: str) -> str:
    """Sanitize apple_id để dùng làm filename component.

    Khớp pattern ``icloud_hme.session._safe_apple_id`` nhưng tách riêng để
    profile_lock không phụ thuộc session module (tránh circular).
    """
    safe = _FILENAME_SAFE_RE.sub("_", apple_id.strip().lower())
    if not safe:
        raise ValueError(f"apple_id rỗng sau sanitize: {apple_id!r}")
    return safe


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


class ProfileLock:
    """Per-apple_id reader-writer lock dùng ``filelock`` + counter file.

    Args:
        lock_dir: Thư mục chứa lock files. Tự ``mkdir(parents=True, exist_ok=True)``.
        apple_id: Apple ID raw (sẽ được sanitize cho filename).

    Note: instance không thread-safe ở mức Python-level, nhưng OS-level
    file lock đảm bảo cross-thread + cross-process. Mỗi caller nên tạo
    instance mới hoặc reuse instance không state mutable.
    """

    def __init__(self, lock_dir: Path, apple_id: str) -> None:
        self.apple_id = apple_id
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        safe = _safe_apple_id_for_lock(apple_id)
        self._writer_path = self.lock_dir / f"icloud-{safe}.write.lock"
        self._sentinel_path = self.lock_dir / f"icloud-{safe}.read.sentinel"
        self._count_path = self.lock_dir / f"icloud-{safe}.read.count"

    # ------------------------------------------------------------------ public

    @contextlib.contextmanager
    def write_lock(self, timeout: float = 30.0) -> Generator[None, None, None]:
        """Exclusive write lock cho Bootstrap_Flow (R12.14) + Recorder (R12.16).

        Block mọi writer khác qua ``writer.lock``, sau đó đợi reader counter
        về 0. Timeout → raise ``ProfileLockError(mode='write', ...)``.
        """
        if timeout <= 0:
            raise ValueError(f"timeout phải > 0, got {timeout}")
        deadline = time.monotonic() + timeout

        writer_lock = FileLock(str(self._writer_path), timeout=timeout)
        try:
            writer_lock.acquire()
        except Timeout:
            raise ProfileLockError(
                self.apple_id, "write", "locked_by_another_process"
            ) from None

        try:
            # Đợi mọi reader release. Mỗi vòng: acquire sentinel ngắn → đọc
            # counter → release sentinel. Reader mới sẽ probe writer.lock
            # (đang được mình giữ) và phải đợi.
            while True:
                count = self._read_counter_via_sentinel(_remaining(deadline))
                if count == 0:
                    break
                if _remaining(deadline) <= 0:
                    raise ProfileLockError(
                        self.apple_id,
                        "write",
                        f"readers_active_after_timeout count={count}",
                    )
                time.sleep(_POLL_INTERVAL_SEC)
            yield
        finally:
            writer_lock.release()

    @contextlib.contextmanager
    def read_lock(self, timeout: float = 60.0) -> Generator[None, None, None]:
        """Shared read lock cho extract_session_bundle (R12.15).

        Multiple reader OK đồng thời. Block khi writer giữ ``writer.lock``.
        Timeout → raise ``ProfileLockError(mode='read',
        reason='profile_locked_by_bootstrap')``.
        """
        if timeout <= 0:
            raise ValueError(f"timeout phải > 0, got {timeout}")
        deadline = time.monotonic() + timeout

        # Spin tới khi writer không giữ → increment counter atomic.
        while True:
            remain = _remaining(deadline)
            if remain <= 0:
                raise ProfileLockError(
                    self.apple_id, "read", "profile_locked_by_bootstrap"
                )
            sentinel = FileLock(str(self._sentinel_path), timeout=remain)
            try:
                sentinel.acquire()
            except Timeout:
                raise ProfileLockError(
                    self.apple_id, "read", "sentinel_acquire_timeout"
                ) from None
            incremented = False
            try:
                if not self._writer_held_unsafe():
                    self._write_counter(self._read_counter_unsafe() + 1)
                    incremented = True
            finally:
                sentinel.release()
            if incremented:
                break
            time.sleep(_POLL_INTERVAL_SEC)

        try:
            yield
        finally:
            self._decrement_counter_via_sentinel()

    # ----------------------------------------------------------------- helpers

    def _writer_held_unsafe(self) -> bool:
        """Probe ``writer.lock`` non-blocking. Caller PHẢI giữ sentinel.

        ``timeout=0`` ép FileLock non-blocking: thành công → release ngay
        (writer không giữ); ``Timeout`` → writer đang giữ.
        """
        probe = FileLock(str(self._writer_path), timeout=0)
        try:
            probe.acquire()
        except Timeout:
            return True
        probe.release()
        return False

    def _read_counter_unsafe(self) -> int:
        """Đọc counter file. Caller giữ sentinel hoặc chấp nhận stale read.

        File chưa tồn tại / rỗng / corrupt → coi như 0 (sẽ overwrite ở lần
        ghi tiếp). Counter file là local ephemeral, không cần migration.
        """
        try:
            text = self._count_path.read_text().strip()
        except FileNotFoundError:
            return 0
        if not text:
            return 0
        try:
            value = int(text)
        except ValueError:
            return 0
        return max(value, 0)

    def _write_counter(self, value: int) -> None:
        if value < 0:
            value = 0
        self._count_path.write_text(str(value))

    def _read_counter_via_sentinel(self, timeout: float) -> int:
        sentinel_timeout = max(timeout, 0.001)
        sentinel = FileLock(str(self._sentinel_path), timeout=sentinel_timeout)
        try:
            sentinel.acquire()
        except Timeout:
            raise ProfileLockError(
                self.apple_id, "write", "sentinel_acquire_timeout"
            ) from None
        try:
            return self._read_counter_unsafe()
        finally:
            sentinel.release()

    def _decrement_counter_via_sentinel(self) -> None:
        # Release path không có deadline meaningful — dùng generous timeout
        # để không leak counter khi sentinel bị ngắn hạn.
        sentinel = FileLock(str(self._sentinel_path), timeout=30.0)
        try:
            sentinel.acquire()
        except Timeout:
            raise ProfileLockError(
                self.apple_id, "read", "sentinel_release_timeout"
            ) from None
        try:
            self._write_counter(self._read_counter_unsafe() - 1)
        finally:
            sentinel.release()
