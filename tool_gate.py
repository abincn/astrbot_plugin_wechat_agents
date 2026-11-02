"""Deny-by-default, durable group-scoped authorization for future trusted tool proxies.

This policy is not an AstrBot-wide sandbox. It only gates tool handlers that
explicitly call ToolGate.execute. AstrBot's on_using_llm_tool hook cannot be
relied on to prevent arbitrary third-party tool execution.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from .identity import group_session

T = TypeVar("T")
TOOL = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")
INTENT = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def args_digest(args: dict) -> str:
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object")
    canonical = json.dumps(args, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    if len(canonical.encode("utf-8")) > 16384:
        raise ValueError("Tool arguments exceed 16KiB")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolDenied(PermissionError):
    pass


class ToolGate:
    def __init__(self, file: str | Path):
        path = Path(file).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tool_policy (
                owner TEXT NOT NULL, room TEXT NOT NULL, tool TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('read_auto','approval')),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                PRIMARY KEY(owner,room,tool)
            );
            CREATE TABLE IF NOT EXISTS tool_intents (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL, room TEXT NOT NULL, tool TEXT NOT NULL,
                digest TEXT NOT NULL, status TEXT NOT NULL,
                created_at INTEGER NOT NULL, completed_at INTEGER,
                error TEXT NOT NULL DEFAULT ''
            );
        """)
        # Crash recovery: a previously started effect could have happened.
        self.conn.execute("UPDATE tool_intents SET status='outcome_unknown', "
                          "error='process_interrupted' WHERE status='executing'")

    def set_policy(self, owner: str, room: str, tool: str, mode: str,
                   enabled: bool) -> None:
        """Administrator-only management API; never expose to LLM or chat users."""
        group_session(owner, room)
        if not TOOL.fullmatch(tool) or mode not in ("read_auto", "approval"):
            raise ValueError("Invalid tool policy")
        with self.lock:
            self.conn.execute("""INSERT INTO tool_policy(owner,room,tool,mode,enabled)
                VALUES(?,?,?,?,?)
                ON CONFLICT(owner,room,tool) DO UPDATE SET
                  mode=excluded.mode,enabled=excluded.enabled""",
                (owner, room, tool, mode, int(enabled)))

    def request(self, intent_id: str, owner: str, room: str, tool: str,
                args: dict) -> str:
        group_session(owner, room)
        if not INTENT.fullmatch(intent_id) or not TOOL.fullmatch(tool):
            raise ValueError("Invalid tool intent")
        digest = args_digest(args)
        with self.lock:
            policy = self.conn.execute(
                "SELECT mode FROM tool_policy WHERE owner=? AND room=? AND tool=? AND enabled=1",
                (owner,room,tool)).fetchone()
            if policy is None:
                raise ToolDenied("Tool not authorized for this group")
            state = "approved" if policy["mode"] == "read_auto" else "needs_approval"
            try:
                self.conn.execute("""INSERT INTO tool_intents
                    (id,owner,room,tool,digest,status,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (intent_id,owner,room,tool,digest,state,int(time.time())))
            except sqlite3.IntegrityError as exc:
                raise ToolDenied("Tool intent already exists") from exc
            return state

    def approve(self, intent_id: str, owner: str, room: str) -> bool:
        """Only a separate authenticated administrator API may invoke this."""
        group_session(owner, room)
        with self.lock:
            cur = self.conn.execute(
                "UPDATE tool_intents SET status='approved' "
                "WHERE id=? AND owner=? AND room=? AND status='needs_approval' "
                "AND EXISTS (SELECT 1 FROM tool_policy p WHERE p.owner=? "
                "AND p.room=? AND p.tool=tool_intents.tool AND p.enabled=1 "
                "AND p.mode='approval')",
                (intent_id,owner,room,owner,room))
            return cur.rowcount == 1

    async def execute(self, intent_id: str, owner: str, room: str, tool: str,
                      args: dict, handler: Callable[[dict], Awaitable[T]]) -> T:
        """Reserve exactly one execution before invoking a trusted handler."""
        group_session(owner,room)
        digest = args_digest(args)
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT status FROM tool_intents WHERE id=? AND owner=? "
                    "AND room=? AND tool=? AND digest=?",
                    (intent_id,owner,room,tool,digest)).fetchone()
                permitted = self.conn.execute(
                    "SELECT 1 FROM tool_policy WHERE owner=? AND room=? "
                    "AND tool=? AND enabled=1",
                    (owner,room,tool)).fetchone()
                if not row or row["status"] != "approved" or not permitted:
                    raise ToolDenied("Tool execution not approved for this group and arguments")
                self.conn.execute(
                    "UPDATE tool_intents SET status='executing' WHERE id=?", (intent_id,))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        # No lock is held during actual tool execution. This is an at-most-once
        # submission: if a call times out, its remote side effect is UNKNOWN.
        try:
            result = await handler(args)
        except BaseException as exc:
            with self.lock:
                self.conn.execute(
                    "UPDATE tool_intents SET status='outcome_unknown',error=?,completed_at=? "
                    "WHERE id=? AND status='executing'",
                    (type(exc).__name__[:120],int(time.time()),intent_id))
            raise
        with self.lock:
            self.conn.execute(
                "UPDATE tool_intents SET status='completed',completed_at=? "
                "WHERE id=? AND status='executing'", (int(time.time()),intent_id))
        return result

    def state(self, intent_id: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT status FROM tool_intents WHERE id=?",
                                    (intent_id,)).fetchone()
            return row["status"] if row else None

    def close(self) -> None:
        with self.lock:
            self.conn.close()
