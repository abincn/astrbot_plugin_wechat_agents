"""Shared SQLite primitives for the Thread/Ticket router."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


def _identity(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:48]


def _required(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return value.strip()


@dataclass(frozen=True)
class TicketAssignment:
    ticket_id: str
    thread_id: str
    owner: str
    room: str
    task: str
    worker: str
    claim_token: str


@dataclass(frozen=True)
class SubscriptionAssignment:
    subscription_id: str
    thread_id: str
    owner: str
    room: str
    task: str
    worker: str
    claim_token: str
    lease_until: int


class RouterCore:
    """Own the shared router database, transaction and scoped row lookups."""

    def __init__(self, inbox_db: str | Path, *,
                 clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        location = Path(inbox_db).expanduser().resolve()
        if not location.is_file():
            raise FileNotFoundError("The existing durable Inbox database is required")
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(location), isolation_level=None, check_same_thread=False,
            timeout=5,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox'"
        ).fetchone():
            self.conn.close()
            raise ValueError("TaskRouter must share an initialized DurableInbox database")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS router_threads (
                thread_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                room TEXT NOT NULL,
                trigger_event_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','completed')),
                created_at INTEGER NOT NULL,
                closed_at INTEGER,
                UNIQUE(owner,room,trigger_event_id)
            );
            CREATE TABLE IF NOT EXISTS router_tickets (
                ticket_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES router_threads(thread_id),
                client_key TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('queued','running','completed','review')),
                assigned_worker TEXT NOT NULL DEFAULT '',
                claim_token TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                review_reason TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(thread_id,client_key)
            );
            CREATE INDEX IF NOT EXISTS router_tickets_status
                ON router_tickets(status,created_at);
            CREATE TABLE IF NOT EXISTS router_evidence (
                evidence_id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL REFERENCES router_tickets(ticket_id),
                source_type TEXT NOT NULL CHECK(source_type IN
                    ('inbox_message','https_url','sha256')),
                source_ref TEXT NOT NULL,
                summary TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(ticket_id,source_type,source_ref)
            );
            CREATE INDEX IF NOT EXISTS router_evidence_ticket
                ON router_evidence(ticket_id,created_at);
            CREATE TABLE IF NOT EXISTS router_subscriptions (
                subscription_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES router_threads(thread_id),
                owner TEXT NOT NULL,
                room TEXT NOT NULL,
                client_key TEXT NOT NULL,
                task TEXT NOT NULL,
                due_at INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('scheduled','paused','claimed','fired','review','cancelled')),
                claim_token TEXT NOT NULL DEFAULT '',
                claimed_by TEXT NOT NULL DEFAULT '',
                lease_until INTEGER,
                claim_attempts INTEGER NOT NULL DEFAULT 0,
                ticket_id TEXT NOT NULL DEFAULT '',
                review_reason TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(thread_id,client_key)
            );
            CREATE INDEX IF NOT EXISTS router_subscriptions_due
                ON router_subscriptions(owner,room,status,due_at,lease_until);
            CREATE TABLE IF NOT EXISTS router_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                ticket_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
        """)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _audit(self, thread_id: str, ticket_id: str, action: str,
               actor: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO router_audit"
            "(thread_id,ticket_id,action,actor,detail,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (thread_id,ticket_id,action,actor,detail[:300],int(self._clock())),
        )

    def _thread(self, thread_id: str, owner: str, room: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM router_threads "
            "WHERE thread_id=? AND owner=? AND room=?",
            (thread_id,owner,room),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown thread for this account/group")
        return row

    def _ticket(self, ticket_id: str, owner: str, room: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT t.* FROM router_tickets t "
            "JOIN router_threads h ON h.thread_id=t.thread_id "
            "WHERE t.ticket_id=? AND h.owner=? AND h.room=?",
            (ticket_id,owner,room),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown ticket for this account/group")
        return row

    def close(self) -> None:
        with self._lock:
            self.conn.close()
