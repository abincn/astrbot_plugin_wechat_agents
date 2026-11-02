"""One-shot subscription persistence and lease state transitions."""
from __future__ import annotations

import hmac
import uuid

from .identity import group_session
from .router_core import RouterCore, SubscriptionAssignment, _identity, _required


class SubscriptionOperations(RouterCore):
    def schedule_subscription(self, owner: str, room: str, thread_id: str,
                              client_key: str, task: str, due_at: int, *,
                              actor: str) -> str:
        """Create an immutable one-shot wake-up; no timer/model/send is started."""
        group_session(owner, room)
        client_key = _required(client_key, "client_key", 160)
        if client_key.startswith("__subscription__:"):
            raise ValueError("Reserved subscription client key prefix")
        task = _required(task, "task", 4096)
        actor = _required(actor, "actor", 120)
        if type(due_at) is not int or due_at <= 0:
            raise ValueError("due_at must be a positive Unix timestamp in seconds")
        subscription_id = "subscription-" + _identity(thread_id, client_key)
        now = int(self._clock())
        with self._transaction():
            thread = self._thread(thread_id, owner, room)
            if thread["status"] != "open":
                raise ValueError("Cannot schedule on a closed Thread")
            old = self.conn.execute(
                "SELECT subscription_id,task,due_at FROM router_subscriptions "
                "WHERE thread_id=? AND client_key=?",
                (thread_id, client_key),
            ).fetchone()
            if old:
                if old["task"] != task or old["due_at"] != due_at:
                    raise ValueError("Conflicting content for immutable Subscription key")
                return old["subscription_id"]
            self.conn.execute(
                "INSERT INTO router_subscriptions"
                "(subscription_id,thread_id,owner,room,client_key,task,due_at,"
                "status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'scheduled',?,?)",
                (subscription_id, thread_id, owner, room, client_key, task,
                 due_at, now, now),
            )
            self._audit(thread_id, "", "subscription_scheduled", actor,
                        f"subscription={subscription_id};due_at={due_at}")
        return subscription_id

    def claim_due_subscriptions(self, owner: str, room: str, *,
                                limit: int = 10, lease_seconds: int = 60,
                                worker: str = "scheduler",
                                actor: str | None = None) -> list[SubscriptionAssignment]:
        """Lease due wake-ups. Expired leases are safely reclaimable (no side effect yet)."""
        group_session(owner, room)
        if worker != "scheduler":
            raise ValueError("Only the scheduler worker is currently supported")
        audit_actor = _required(actor or worker, "actor", 120)
        now = int(self._clock())
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 10 and 900")
        assignments = []
        with self._transaction():
            rows = self.conn.execute(
                "SELECT * FROM router_subscriptions WHERE owner=? AND room=? AND "
                "((status='scheduled' AND due_at<=?) OR "
                "(status='claimed' AND lease_until<=?)) "
                "ORDER BY due_at,created_at LIMIT ?",
                (owner, room, now, now, limit),
            ).fetchall()
            for row in rows:
                if row["claim_attempts"] >= 5:
                    self.conn.execute(
                        "UPDATE router_subscriptions SET status='review',claim_token='',"
                        "claimed_by='',lease_until=NULL,"
                        "review_reason='lease_claim_limit_reached',updated_at=? "
                        "WHERE subscription_id=?",
                        (now, row["subscription_id"]),
                    )
                    self._audit(row["thread_id"], "", "subscription_needs_review",
                                audit_actor, "lease claim limit reached")
                    continue
                token = uuid.uuid4().hex
                lease_until = now + lease_seconds
                action = "subscription_reclaimed" if row["status"] == "claimed" else "subscription_claimed"
                self.conn.execute(
                    "UPDATE router_subscriptions SET status='claimed',claim_token=?,"
                    "claimed_by=?,lease_until=?,claim_attempts=claim_attempts+1,"
                    "updated_at=? WHERE subscription_id=?",
                    (token, worker, lease_until, now, row["subscription_id"]),
                )
                self._audit(row["thread_id"], "", action, audit_actor,
                            f"subscription={row['subscription_id']};lease_until={lease_until}")
                assignments.append(SubscriptionAssignment(
                    row["subscription_id"], row["thread_id"], owner, room,
                    row["task"], worker, token, lease_until,
                ))
        return assignments

    def fire_subscription(self, assignment: SubscriptionAssignment, *,
                          actor: str) -> str | None:
        """Atomically turn a valid leased wake-up into one queued Ticket.

        No model, external API, Outbox or WeChat send occurs. A closed Thread
        or legacy key collision is moved to review and returns None.
        """
        group_session(assignment.owner, assignment.room)
        actor = _required(actor, "actor", 120)
        if assignment.worker != "scheduler":
            raise ValueError("Invalid Subscription worker")
        now = int(self._clock())
        with self._transaction():
            row = self.conn.execute(
                "SELECT * FROM router_subscriptions WHERE subscription_id=? "
                "AND owner=? AND room=?",
                (assignment.subscription_id, assignment.owner, assignment.room),
            ).fetchone()
            if (
                row is None or row["status"] != "claimed"
                or row["claimed_by"] != "scheduler"
                or row["thread_id"] != assignment.thread_id
                or row["task"] != assignment.task
                or not hmac.compare_digest(row["claim_token"], assignment.claim_token)
                or row["lease_until"] is None or row["lease_until"] <= now
            ):
                raise ValueError("Stale or expired Subscription assignment")
            thread = self._thread(row["thread_id"], assignment.owner, assignment.room)
            if thread["status"] != "open":
                self.conn.execute(
                    "UPDATE router_subscriptions SET status='review',claim_token='',"
                    "claimed_by='',lease_until=NULL,review_reason='thread_closed',updated_at=? "
                    "WHERE subscription_id=?",
                    (now, assignment.subscription_id),
                )
                self._audit(row["thread_id"], "", "subscription_needs_review",
                            actor, "thread closed before wake-up")
                return None

            client_key = "__subscription__:" + assignment.subscription_id
            existing = self.conn.execute(
                "SELECT ticket_id,task FROM router_tickets "
                "WHERE thread_id=? AND client_key=?",
                (row["thread_id"], client_key),
            ).fetchone()
            if existing and existing["task"] != row["task"]:
                self.conn.execute(
                    "UPDATE router_subscriptions SET status='review',claim_token='',"
                    "claimed_by='',lease_until=NULL,review_reason='ticket_key_conflict',"
                    "updated_at=? WHERE subscription_id=?",
                    (now, assignment.subscription_id),
                )
                self._audit(row["thread_id"], "", "subscription_needs_review",
                            actor, "reserved Ticket key conflict")
                return None
            if existing:
                ticket_id = existing["ticket_id"]
            else:
                ticket_id = "ticket-" + _identity(row["thread_id"], client_key)
                self.conn.execute(
                    "INSERT INTO router_tickets"
                    "(ticket_id,thread_id,client_key,task,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,'queued',?,?)",
                    (ticket_id, row["thread_id"], client_key, row["task"], now, now),
                )
                self._audit(row["thread_id"], ticket_id, "ticket_queued",
                            actor, "created by Subscription wake-up")
            self.conn.execute(
                "UPDATE router_subscriptions SET status='fired',claim_token='',"
                "claimed_by='',lease_until=NULL,ticket_id=?,review_reason='',updated_at=? "
                "WHERE subscription_id=?",
                (ticket_id, now, assignment.subscription_id),
            )
            self._audit(row["thread_id"], ticket_id, "subscription_fired",
                        actor, f"subscription={assignment.subscription_id}")
            return ticket_id

    def pause_subscription(self, owner: str, room: str, subscription_id: str, *,
                           actor: str) -> None:
        group_session(owner, room)
        actor = _required(actor, "actor", 120)
        with self._transaction():
            row = self.conn.execute(
                "SELECT * FROM router_subscriptions WHERE subscription_id=? "
                "AND owner=? AND room=?",
                (subscription_id, owner, room),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown Subscription for this account/group")
            if row["status"] not in ("scheduled", "claimed"):
                raise ValueError("Only scheduled or claimed Subscriptions can be paused")
            self.conn.execute(
                "UPDATE router_subscriptions SET status='paused',claim_token='',"
                "claimed_by='',lease_until=NULL,updated_at=? WHERE subscription_id=?",
                (int(self._clock()), subscription_id),
            )
            self._audit(row["thread_id"], "", "subscription_paused", actor,
                        f"subscription={subscription_id}")

    def resume_subscription(self, owner: str, room: str, subscription_id: str, *,
                            actor: str) -> None:
        group_session(owner, room)
        actor = _required(actor, "actor", 120)
        with self._transaction():
            row = self.conn.execute(
                "SELECT * FROM router_subscriptions WHERE subscription_id=? "
                "AND owner=? AND room=?",
                (subscription_id, owner, room),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown Subscription for this account/group")
            if row["status"] != "paused":
                raise ValueError("Only paused Subscriptions can be resumed")
            self.conn.execute(
                "UPDATE router_subscriptions SET status='scheduled',updated_at=? "
                "WHERE subscription_id=?",
                (int(self._clock()), subscription_id),
            )
            self._audit(row["thread_id"], "", "subscription_resumed", actor,
                        f"subscription={subscription_id}")

    def cancel_subscription(self, owner: str, room: str, subscription_id: str, *,
                            actor: str) -> None:
        group_session(owner, room)
        actor = _required(actor, "actor", 120)
        with self._transaction():
            row = self.conn.execute(
                "SELECT * FROM router_subscriptions WHERE subscription_id=? "
                "AND owner=? AND room=?",
                (subscription_id, owner, room),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown Subscription for this account/group")
            if row["status"] not in ("scheduled", "paused", "claimed", "review"):
                raise ValueError("Only pending or review Subscriptions can be cancelled")
            self.conn.execute(
                "UPDATE router_subscriptions SET status='cancelled',claim_token='',"
                "claimed_by='',lease_until=NULL,updated_at=? WHERE subscription_id=?",
                (int(self._clock()), subscription_id),
            )
            self._audit(row["thread_id"], "", "subscription_cancelled", actor,
                        f"subscription={subscription_id}")

    def get_subscription(self, owner: str, room: str,
                         subscription_id: str) -> dict:
        group_session(owner, room)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM router_subscriptions WHERE subscription_id=? "
                "AND owner=? AND room=?",
                (subscription_id, owner, room),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown Subscription for this account/group")
            result = dict(row)
            result.pop("claim_token", None)
            return result
