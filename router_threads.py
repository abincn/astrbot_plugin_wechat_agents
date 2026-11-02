"""Thread promotion, completion and scoped inspection."""
from __future__ import annotations

from .identity import group_session
from .router_core import RouterCore, _identity, _required


class ThreadOperations(RouterCore):
    def open_thread(self, owner: str, room: str, trigger_event_id: str,
                    goal: str, *, actor: str) -> str:
        """Explicit promotion only: never create tasks from arbitrary text.

        A trigger must already belong to this group in DurableInbox.
        Duplicate promotion returns the original immutable Thread, but a
        changed goal for the same trigger fails instead of overwriting it.
        """
        group_session(owner,room)
        trigger_event_id = _required(trigger_event_id,"trigger_event_id",220)
        goal = _required(goal,"goal",4096)
        actor = _required(actor,"actor",120)
        thread_id = "thread-" + _identity(owner,room,trigger_event_id)
        with self._transaction():
            source = self.conn.execute(
                "SELECT kind FROM inbox WHERE event_id=? AND owner=? AND room=?",
                (trigger_event_id,owner,room),
            ).fetchone()
            if source is None:
                raise ValueError("Trigger is not a persisted message in this group")
            if source["kind"] != 1:
                raise ValueError("Only text-message triggers are supported")
            old = self.conn.execute(
                "SELECT goal FROM router_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if old:
                if old["goal"] != goal:
                    raise ValueError("Conflicting goal for the same immutable trigger")
                return thread_id
            self.conn.execute(
                "INSERT INTO router_threads"
                "(thread_id,owner,room,trigger_event_id,goal,status,created_at) "
                "VALUES(?,?,?,?,?,'open',?)",
                (thread_id,owner,room,trigger_event_id,goal,int(self._clock())),
            )
            self._audit(thread_id,"","thread_opened",actor)
        return thread_id

    def complete_thread(self, owner: str, room: str, thread_id: str, *,
                        reviewer: str) -> None:
        group_session(owner,room)
        reviewer = _required(reviewer,"reviewer",120)
        with self._transaction():
            row = self._thread(thread_id,owner,room)
            if row["status"] != "open":
                raise ValueError("Thread is already completed")
            counts = self.conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS done "
                "FROM router_tickets WHERE thread_id=?", (thread_id,),
            ).fetchone()
            if not counts["total"] or counts["total"] != counts["done"]:
                raise ValueError("Thread has uncompleted or unresolved Tickets")
            active_subscriptions = self.conn.execute(
                "SELECT COUNT(*) FROM router_subscriptions WHERE thread_id=? "
                "AND status IN ('scheduled','paused','claimed','review')",
                (thread_id,),
            ).fetchone()[0]
            if active_subscriptions:
                raise ValueError("Thread has active or unresolved Subscriptions")
            self.conn.execute(
                "UPDATE router_threads SET status='completed',closed_at=? "
                "WHERE thread_id=?",
                (int(self._clock()),thread_id),
            )
            self._audit(thread_id,"","thread_completed",reviewer)

    def get_thread(self, owner: str, room: str, thread_id: str) -> dict:
        group_session(owner,room)
        with self._lock:
            return dict(self._thread(thread_id,owner,room))

    def audit(self, owner: str, room: str, thread_id: str) -> list[dict]:
        group_session(owner,room)
        with self._lock:
            self._thread(thread_id,owner,room)
            return [dict(r) for r in self.conn.execute(
                "SELECT action,actor,ticket_id,detail,created_at "
                "FROM router_audit WHERE thread_id=? ORDER BY id",(thread_id,)
            )]
