from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


ROLES = {"viewer": 10, "editor": 20, "owner": 30}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    role: str
    owner: bool = False

    def public_dict(self) -> dict:
        return asdict(self)


class AccessStore:
    """Small, durable access-control store.

    SQLite is deliberately used here even while quizzes remain in their legacy JSON
    store: tokens need transactions and revocation, and no plaintext collaborator
    secret is ever persisted.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collaborators (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('viewer', 'editor')),
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_events_created
                    ON audit_events(id DESC);
                """
            )

    def authenticate(self, token: str) -> Principal | None:
        if not token:
            return None
        digest = _token_hash(token)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT id, name, role FROM collaborators
                   WHERE token_hash = ? AND revoked_at IS NULL""",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE collaborators SET last_used_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        return Principal(id=row["id"], name=row["name"], role=row["role"])

    def create(self, name: str, role: str) -> tuple[Principal, str]:
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > 80:
            raise ValueError("Имя должно содержать от 1 до 80 символов")
        if role not in {"viewer", "editor"}:
            raise ValueError("Роль должна быть editor или viewer")
        collaborator_id = secrets.token_urlsafe(10)
        token = "qz_" + secrets.token_urlsafe(32)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO collaborators
                   (id, name, role, token_hash, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (collaborator_id, clean_name, role, _token_hash(token), _now()),
            )
        return Principal(collaborator_id, clean_name, role), token

    def list(self) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT id, name, role, created_at, last_used_at, revoked_at
                   FROM collaborators ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, collaborator_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE collaborators SET revoked_at = ?
                   WHERE id = ? AND revoked_at IS NULL""",
                (_now(), collaborator_id),
            )
            return cursor.rowcount > 0

    def audit(self, principal: Principal, action: str, target: str = "", details: str = "") -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO audit_events
                   (created_at, actor_id, actor_name, action, target, details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_now(), principal.id, principal.name, action[:80], target[:200], details[:1000]),
            )

    def audit_log(self, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT id, created_at, actor_id, actor_name, action, target, details
                   FROM audit_events ORDER BY id DESC LIMIT ?""",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def role_allows(actual: str, required: str) -> bool:
    return ROLES.get(actual, 0) >= ROLES.get(required, 999)
