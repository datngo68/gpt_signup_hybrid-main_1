"""Repository layer cho iCloud HME — Apple ID accounts + generated emails."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

# Dual-import: relative khi package được import dotted
# (`gpt_signup_hybrid.icloud_hme.repository`); absolute fallback khi import flat
# (sys.path chỉ có repo root, package `gpt_signup_hybrid` không nằm trên sys.path).
try:
    from db.engine import DatabaseError
except ImportError:  # pragma: no cover — flat import path
    from db.engine import DatabaseError  # type: ignore[no-redef]

if TYPE_CHECKING:
    try:
        from db.engine import DatabaseEngine
    except ImportError:
        from db.engine import DatabaseEngine  # type: ignore[no-redef]


# Hard cap của Apple cho HME: ~750/account đời account.
# Conservative threshold để tránh chạm tường rồi mới fail.
HME_QUOTA_LIMIT = 720


class IcloudRepositoryError(DatabaseError):
    """iCloud repository operation failure."""

    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"{operation} failed: {cause}")


class IcloudPoolError(Exception):
    """Pool exhausted hoặc không pick được account khả dụng."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IcloudAccountRepository:
    """Data access cho `icloud_accounts` table."""

    def __init__(self, engine: "DatabaseEngine") -> None:
        self._engine = engine

    def get(self, apple_id: str) -> dict | None:
        conn = self._engine.raw_connection()
        row = conn.execute(
            "SELECT * FROM icloud_accounts WHERE apple_id = ?", (apple_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict]:
        conn = self._engine.raw_connection()
        rows = conn.execute(
            "SELECT * FROM icloud_accounts ORDER BY created_at ASC",
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert(self, apple_id: str, profile_dir: str) -> None:
        """Insert account mới, hoặc update profile_dir nếu đã tồn tại.

        KHÔNG reset hme_count / disabled — preserve state pool.
        """
        try:
            with self._engine.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO icloud_accounts (apple_id, profile_dir)
                    VALUES (?, ?)
                    ON CONFLICT(apple_id) DO UPDATE SET
                        profile_dir = excluded.profile_dir
                    """,
                    (apple_id, profile_dir),
                )
        except Exception as exc:
            raise IcloudRepositoryError("upsert", exc) from exc

    def reactivate(self, apple_id: str) -> None:
        """Reset disabled=0 + last_error=NULL. Gọi sau khi user re-bootstrap thành công.

        KHÔNG đổi hme_count — quota đã dùng vẫn được track (Apple cap đời account).
        """
        try:
            with self._engine.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE icloud_accounts
                    SET disabled = 0,
                        last_error = NULL
                    WHERE apple_id = ?
                    """,
                    (apple_id,),
                )
                if cursor.rowcount == 0:
                    raise IcloudRepositoryError(
                        "reactivate",
                        ValueError(f"apple_id không tồn tại: {apple_id}"),
                    )
        except IcloudRepositoryError:
            raise
        except Exception as exc:
            raise IcloudRepositoryError("reactivate", exc) from exc

    def increment_hme_count(self, apple_id: str, *, by: int = 1) -> int:
        """Tăng hme_count, trả về count mới. Set last_used_at = now."""
        try:
            with self._engine.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE icloud_accounts
                    SET hme_count = hme_count + ?,
                        last_used_at = ?
                    WHERE apple_id = ?
                    """,
                    (by, _utc_now(), apple_id),
                )
                if cursor.rowcount == 0:
                    raise IcloudRepositoryError(
                        "increment_hme_count",
                        ValueError(f"apple_id không tồn tại: {apple_id}"),
                    )
                row = conn.execute(
                    "SELECT hme_count FROM icloud_accounts WHERE apple_id = ?",
                    (apple_id,),
                ).fetchone()
                return int(row["hme_count"])
        except IcloudRepositoryError:
            raise
        except Exception as exc:
            raise IcloudRepositoryError("increment_hme_count", exc) from exc

    def mark_disabled(self, apple_id: str, error: str) -> None:
        """Disable account (quota hết / cookie expired / 2FA bắt lại)."""
        try:
            with self._engine.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE icloud_accounts
                    SET disabled = 1,
                        last_error = ?,
                        last_used_at = ?
                    WHERE apple_id = ?
                    """,
                    (error, _utc_now(), apple_id),
                )
                if cursor.rowcount == 0:
                    raise IcloudRepositoryError(
                        "mark_disabled",
                        ValueError(f"apple_id không tồn tại: {apple_id}"),
                    )
        except IcloudRepositoryError:
            raise
        except Exception as exc:
            raise IcloudRepositoryError("mark_disabled", exc) from exc

    def pick_available(self) -> dict:
        """Pick account đầu tiên: chưa disabled + hme_count < HME_QUOTA_LIMIT.

        Order: hme_count ASC (account còn nhiều quota nhất trước),
        rồi created_at ASC (account cũ trước).
        """
        conn = self._engine.raw_connection()
        row = conn.execute(
            """
            SELECT * FROM icloud_accounts
            WHERE disabled = 0
              AND hme_count < ?
            ORDER BY hme_count ASC, created_at ASC
            LIMIT 1
            """,
            (HME_QUOTA_LIMIT,),
        ).fetchone()
        if row is None:
            total = conn.execute(
                "SELECT COUNT(*) FROM icloud_accounts",
            ).fetchone()[0]
            disabled_count = conn.execute(
                "SELECT COUNT(*) FROM icloud_accounts WHERE disabled = 1",
            ).fetchone()[0]
            quota_full = conn.execute(
                "SELECT COUNT(*) FROM icloud_accounts WHERE hme_count >= ?",
                (HME_QUOTA_LIMIT,),
            ).fetchone()[0]
            raise IcloudPoolError(
                f"không còn iCloud account khả dụng "
                f"(total={total}, disabled={disabled_count}, quota_full={quota_full}). "
                f"Chạy bootstrap thêm Apple ID mới."
            )
        return dict(row)


class IcloudEmailRepository:
    """Data access cho `icloud_emails` table."""

    def __init__(self, engine: "DatabaseEngine") -> None:
        self._engine = engine

    def insert(
        self,
        *,
        email: str,
        apple_id: str,
        label: str | None = None,
        note: str | None = None,
        hme_id: str | None = None,
    ) -> int:
        """Insert email mới (status='created'). Return auto id."""
        try:
            with self._engine.get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO icloud_emails (email, apple_id, label, note, hme_id, status)
                    VALUES (?, ?, ?, ?, ?, 'created')
                    """,
                    (email, apple_id, label, note, hme_id),
                )
                return int(cursor.lastrowid)
        except Exception as exc:
            raise IcloudRepositoryError("insert", exc) from exc

    def list_by_status(self, status: str, *, limit: int | None = None) -> list[dict]:
        conn = self._engine.raw_connection()
        sql = (
            "SELECT * FROM icloud_emails WHERE status = ? "
            "ORDER BY datetime(created_at) ASC, id ASC"
        )
        params: list[object] = [status]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_all(self, *, limit: int | None = None) -> list[dict]:
        conn = self._engine.raw_connection()
        sql = "SELECT * FROM icloud_emails ORDER BY datetime(created_at) DESC, id DESC"
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        conn = self._engine.raw_connection()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM icloud_emails GROUP BY status",
        ).fetchall()
        return {row["status"]: int(row["cnt"]) for row in rows}

    def mark_used(self, email: str, used_for_email: str) -> None:
        """Đánh dấu email đã được dùng (cho ChatGPT signup ...). Caller bên ngoài gọi."""
        try:
            with self._engine.get_connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE icloud_emails
                    SET status = 'used',
                        used_for_email = ?,
                        used_at = ?
                    WHERE email = ? AND status = 'created'
                    """,
                    (used_for_email, _utc_now(), email),
                )
                if cursor.rowcount == 0:
                    raise IcloudRepositoryError(
                        "mark_used",
                        ValueError(
                            f"email {email} không tồn tại hoặc không ở status 'created'"
                        ),
                    )
        except IcloudRepositoryError:
            raise
        except Exception as exc:
            raise IcloudRepositoryError("mark_used", exc) from exc


def get_icloud_repos(
    engine: "DatabaseEngine",
) -> tuple[IcloudAccountRepository, IcloudEmailRepository]:
    """Factory tiện lợi cho icloud_hme module."""
    return IcloudAccountRepository(engine), IcloudEmailRepository(engine)
