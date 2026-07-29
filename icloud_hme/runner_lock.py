"""Cross-process single-instance lock cho HmeRunner.

Mục đích:
    - HmeRunner._running guard chỉ in-process. Nếu CLI ``icloud generate``
      và Web server (POST /api/icloud/run) cùng chạy → 2 process cùng
      reserve profile từ DB pool → race ở ``IcloudPoolManager.reserve()``
      level transaction.
    - Multi-worker uvicorn (--workers >= 2) cũng chạy 2 process độc lập;
      mỗi process có ``_runner`` singleton riêng → user press Stop có thể
      tới worker khác → no-op.

Cơ chế:
    - File lock ở ``<runtime_dir>/icloud_runner.lock``.
    - Acquire qua ``fcntl.flock(LOCK_EX | LOCK_NB)`` — non-blocking, fail
      ngay nếu đã có process khác giữ lock. macOS / Linux đều support.
    - Lock release tự động khi process chết (kernel-level), không cần
      cleanup pidfile bằng tay → an toàn với SIGKILL/crash.
    - Ghi PID vào file để debug ``ps -p $(cat icloud_runner.lock)``.

Usage:
    lock = RunnerLock(settings.runtime_dir)
    try:
        lock.acquire()
        # ... runner logic ...
    finally:
        lock.release()

Hoặc dùng context manager:
    with RunnerLock(settings.runtime_dir) as lock:
        ...

KHÔNG default insecure: file lock CHỈ enforce single-instance khi caller
chủ động gọi ``acquire()``. Module không tự gắn vào HmeRunner để giữ
tính reusable (Runner có thể test mà không cần lock).
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from typing import Optional


_LOCK_FILENAME: str = "icloud_runner.lock"


class RunnerLockError(RuntimeError):
    """Lock acquire failed (process khác đang giữ lock).

    Attributes:
        existing_pid: PID của process đang giữ lock (đọc từ pidfile, có thể
            None nếu file rỗng/lỗi đọc).
        lock_path: Đường dẫn file lock để user/operator soi.
    """

    def __init__(self, lock_path: Path, existing_pid: Optional[int]) -> None:
        self.lock_path = lock_path
        self.existing_pid = existing_pid
        msg = (
            f"Process khác đang chạy HmeRunner (lock: {lock_path}"
            + (f", pid={existing_pid}" if existing_pid is not None else "")
            + "). Dừng process đó trước khi start lại."
        )
        super().__init__(msg)


class RunnerLock:
    """File lock cross-process single-instance cho HmeRunner.

    Lifecycle:
        1. ``__init__(runtime_dir)``: tính ``lock_path`` = ``<runtime>/<file>``.
        2. ``acquire()``: open file (create nếu thiếu), ``flock(LOCK_EX |
           LOCK_NB)``, ghi PID vào file. Raise ``RunnerLockError`` nếu
           process khác đang giữ.
        3. ``release()``: ``flock(LOCK_UN)``, close file. KHÔNG xoá file
           (tránh race với process kế tiếp đang chuẩn bị acquire).

    KHÔNG thread-safe — caller chỉ nên gọi ``acquire()`` từ 1 thread (asyncio
    main thread đã đủ).
    """

    def __init__(self, runtime_dir: Path) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._lock_path = self._runtime_dir / _LOCK_FILENAME
        self._fd: Optional[int] = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Acquire lock non-blocking. Raise ``RunnerLockError`` nếu fail.

        Idempotent: gọi 2 lần liên tiếp trong cùng instance không acquire
        lại — return im lặng nếu ``_fd`` đã set.
        """
        if self._fd is not None:
            return  # idempotent

        # Tạo runtime_dir nếu chưa có (best-effort, lỗi permission để raise).
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

        # Open với O_CREAT | O_RDWR để đọc PID cũ + ghi PID mới. KHÔNG
        # truncate vì cần đọc PID cũ trước khi acquire fail.
        fd = os.open(
            str(self._lock_path),
            os.O_RDWR | os.O_CREAT,
            0o600,  # rw cho owner, không leak quyền
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # EAGAIN / EWOULDBLOCK → process khác đang giữ.
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                existing_pid = self._read_pid_from_fd(fd)
                os.close(fd)
                raise RunnerLockError(self._lock_path, existing_pid) from None
            os.close(fd)
            raise

        # Lock đã giữ — ghi PID hiện tại để debug.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError:
            # Ghi PID fail không phải lỗi nghiêm trọng — lock vẫn giữ. Chỉ
            # giảm khả năng debug. KHÔNG release để giữ invariant.
            pass

        self._fd = fd

    def release(self) -> None:
        """Release lock + close fd. Idempotent."""
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # best-effort
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "RunnerLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    @staticmethod
    def _read_pid_from_fd(fd: int) -> Optional[int]:
        """Đọc PID từ file đã open. Return None nếu rỗng/parse fail."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 64).decode("ascii", errors="ignore").strip()
            if not data:
                return None
            return int(data.split()[0])
        except (OSError, ValueError):
            return None


__all__ = ["RunnerLock", "RunnerLockError"]
