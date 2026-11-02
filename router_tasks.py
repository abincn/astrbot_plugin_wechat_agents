"""Ticket lifecycle state transitions; Thread API inherited for compatibility."""
from __future__ import annotations

import hmac
import uuid

from .identity import group_session
from .router_core import TicketAssignment, _identity, _required
from .router_threads import ThreadOperations


class TaskOperations(ThreadOperations):
    def enqueue_ticket(self, owner: str, room: str, thread_id: str,
                       client_key: str, task: str, *, actor: str) -> str:
        group_session(owner,room)
        client_key = _required(client_key,"client_key",160)
        if client_key.startswith("__subscription__:"):
            raise ValueError("Reserved subscription Ticket key prefix")
        task = _required(task,"task",4096)
        actor = _required(actor,"actor",120)
        with self._transaction():
            thread = self._thread(thread_id,owner,room)
            old = self.conn.execute(
                "SELECT ticket_id,task FROM router_tickets "
                "WHERE thread_id=? AND client_key=?",
                (thread_id,client_key),
            ).fetchone()
            if old:
                if old["task"] != task:
                    raise ValueError("Conflicting content for immutable ticket key")
                return old["ticket_id"]
            if thread["status"] != "open":
                raise ValueError("Cannot add Tickets to a closed Thread")
            ticket_id = "ticket-" + _identity(thread_id,client_key)
            self.conn.execute(
                "INSERT INTO router_tickets"
                "(ticket_id,thread_id,client_key,task,status,created_at,updated_at) "
                "VALUES(?,?,?,?,'queued',?,?)",
                (ticket_id,thread_id,client_key,task,int(self._clock()),int(self._clock())),
            )
            self._audit(thread_id,ticket_id,"ticket_queued",actor)
        return ticket_id

    def claim_next_ticket(self, owner: str, room: str, *,
                          actor: str = "astrbot") -> TicketAssignment | None:
        """Atomically claim the oldest queued Ticket in one open Thread."""
        group_session(owner, room)
        actor = _required(actor, "actor", 120)
        with self._transaction():
            row = self.conn.execute(
                "SELECT t.* FROM router_tickets t "
                "JOIN router_threads h ON h.thread_id=t.thread_id "
                "WHERE h.owner=? AND h.room=? AND h.status='open' "
                "AND t.status='queued' "
                "ORDER BY t.created_at,t.ticket_id LIMIT 1",
                (owner, room),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            updated = self.conn.execute(
                "UPDATE router_tickets SET status='running',assigned_worker='astrbot',"
                "claim_token=?,updated_at=? WHERE ticket_id=? AND status='queued'",
                (token, int(self._clock()), row["ticket_id"]),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Ticket was claimed concurrently")
            self._audit(row["thread_id"], row["ticket_id"],
                        "ticket_claimed", actor)
            return TicketAssignment(
                row["ticket_id"], row["thread_id"], owner, room,
                row["task"], "astrbot", token,
            )

    def claim_ticket(self, owner: str, room: str, ticket_id: str,
                     *, worker: str = "astrbot") -> TicketAssignment:
        group_session(owner,room)
        # Only the already-integrated AstrBot worker is admitted at this stage.
        if worker != "astrbot":
            raise ValueError("Only the AstrBot worker is currently supported")
        with self._transaction():
            row = self._ticket(ticket_id,owner,room)
            if row["status"] != "queued":
                raise ValueError("Ticket is not available for assignment")
            token = uuid.uuid4().hex
            updated = self.conn.execute(
                "UPDATE router_tickets SET status='running',assigned_worker=?,"
                "claim_token=?,updated_at=? "
                "WHERE ticket_id=? AND status='queued'",
                (worker,token,int(self._clock()),ticket_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Ticket was claimed concurrently")
            self._audit(row["thread_id"],ticket_id,"ticket_claimed",worker)
            return TicketAssignment(ticket_id,row["thread_id"],owner,room,
                                    row["task"],worker,token)

    def verify_assignment(self, assignment: TicketAssignment) -> None:
        """Fail closed before spending provider tokens on a stale/forged claim.

        This check is advisory against a concurrent reconciliation; the final
        submit_candidate() transaction independently rechecks the token.
        """
        if not isinstance(assignment, TicketAssignment):
            raise ValueError("TicketAssignment required")
        group_session(assignment.owner, assignment.room)
        with self._lock:
            row = self._ticket(
                assignment.ticket_id, assignment.owner, assignment.room
            )
            thread = self._thread(
                assignment.thread_id, assignment.owner, assignment.room
            )
            if (
                row["status"] != "running"
                or thread["status"] != "open"
                or row["assigned_worker"] != "astrbot"
                or assignment.worker != "astrbot"
                or row["thread_id"] != assignment.thread_id
                or row["task"] != assignment.task
                or not isinstance(assignment.claim_token, str)
                or not hmac.compare_digest(
                    row["claim_token"], assignment.claim_token
                )
            ):
                raise ValueError("Stale or mismatched Ticket assignment")

    def finish_ticket(self, assignment: TicketAssignment, *,
                      success: bool, result: str) -> None:
        group_session(assignment.owner,assignment.room)
        result = _required(result,"result",30000)
        with self._transaction():
            row = self._ticket(assignment.ticket_id,assignment.owner,assignment.room)
            if (row["status"] != "running" or row["assigned_worker"] != "astrbot"
                    or not hmac.compare_digest(row["claim_token"],assignment.claim_token)
                    or row["thread_id"] != assignment.thread_id):
                raise ValueError("Stale or mismatched Ticket assignment")
            status = "completed" if success else "review"
            self.conn.execute(
                "UPDATE router_tickets SET status=?,claim_token='',result=?,"
                "review_reason=?,updated_at=? WHERE ticket_id=?",
                (status,result,"" if success else "Worker reported failure",
                 int(self._clock()),assignment.ticket_id),
            )
            self._audit(row["thread_id"],assignment.ticket_id,
                        "ticket_completed" if success else "ticket_needs_review",
                        "astrbot")

    def submit_candidate(self, assignment: TicketAssignment, *, result: str) -> None:
        """Store an AI result for independent evaluation, never auto-complete.

        A model response is not evidence that the task succeeded. The claim
        token is checked inside the same transaction as the update so a
        reconciled or reassigned worker cannot publish a stale candidate.
        """
        group_session(assignment.owner, assignment.room)
        result = _required(result, "candidate result", 30000)
        with self._transaction():
            row = self._ticket(assignment.ticket_id, assignment.owner, assignment.room)
            if (
                row["status"] != "running"
                or row["assigned_worker"] != "astrbot"
                or row["thread_id"] != assignment.thread_id
                or not hmac.compare_digest(row["claim_token"], assignment.claim_token)
                or row["task"] != assignment.task
            ):
                raise ValueError("Stale or mismatched Ticket assignment")
            self.conn.execute(
                "UPDATE router_tickets SET status='review',claim_token='',"
                "result=?,review_reason='candidate_requires_eval',updated_at=? "
                "WHERE ticket_id=? AND status='running'",
                (result, int(self._clock()), assignment.ticket_id),
            )
            self._audit(row["thread_id"], assignment.ticket_id,
                        "candidate_submitted", "astrbot")

    def evaluate_candidate(self, owner: str, room: str, ticket_id: str, *,
                           reviewer: str, accepted: bool, reason: str) -> None:
        """Legacy internal decision primitive retained for compatibility.

        New operator interfaces must use record_evidence() followed by
        approve_candidate(); this method does not enforce evidence gating.
        """
        group_session(owner, room)
        if type(accepted) is not bool:
            raise ValueError("accepted must be boolean")
        reviewer = _required(reviewer, "reviewer", 120)
        reason = _required(reason, "reason", 1000)
        with self._transaction():
            row = self._ticket(ticket_id, owner, room)
            if (
                row["status"] != "review"
                or row["review_reason"] not in (
                    "candidate_requires_eval", "candidate_rejected"
                )
                or not row["result"]
            ):
                raise ValueError("Ticket has no reviewable candidate")
            state = "completed" if accepted else "review"
            review_reason = "" if accepted else "candidate_rejected"
            self.conn.execute(
                "UPDATE router_tickets SET status=?,review_reason=?,"
                "updated_at=? WHERE ticket_id=? AND status='review'",
                (state, review_reason, int(self._clock()), ticket_id),
            )
            self._audit(row["thread_id"], ticket_id,
                        "candidate_approved" if accepted else "candidate_rejected",
                        reviewer, reason)

    def reconcile_interrupted(self, *, actor: str) -> int:
        """Call ONLY after exclusive startup fencing of previous workers.

        Results may have been computed before a crash; they are *uncertain*.
        Never auto-replay or assume completion.
        """
        actor = _required(actor,"actor",120)
        with self._transaction():
            rows = self.conn.execute(
                "SELECT ticket_id,thread_id FROM router_tickets "
                "WHERE status='running'"
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE router_tickets SET status='review',claim_token='',"
                    "review_reason='interrupted_unknown_result',updated_at=? "
                    "WHERE ticket_id=? AND status='running'",
                    (int(self._clock()),row["ticket_id"]),
                )
                self._audit(row["thread_id"],row["ticket_id"],
                            "ticket_interrupted_review",actor)
            return len(rows)

    def requeue_review(self, owner: str, room: str, ticket_id: str, *,
                       reviewer: str, reason: str) -> None:
        group_session(owner,room)
        reviewer = _required(reviewer,"reviewer",120)
        reason = _required(reason,"reason",1000)
        with self._transaction():
            row = self._ticket(ticket_id,owner,room)
            if row["status"] != "review":
                raise ValueError("Only reviewed Tickets can be manually requeued")
            self.conn.execute(
                "UPDATE router_tickets SET status='queued',assigned_worker='',"
                "claim_token='',result='',review_reason='',updated_at=? "
                "WHERE ticket_id=?",
                (int(self._clock()),ticket_id),
            )
            self._audit(row["thread_id"],ticket_id,"ticket_requeued",reviewer,reason)

    def list_review_tickets(self, owner: str, room: str, *,
                             limit: int = 50) -> list[dict]:
        """Return a bounded, scoped review queue without worker claim tokens."""
        group_session(owner, room)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            return [dict(row) for row in self.conn.execute(
                "SELECT t.ticket_id,t.thread_id,t.client_key,t.task,t.status,"
                "t.result,t.review_reason,t.created_at,t.updated_at,"
                "h.goal AS thread_goal,h.trigger_event_id,"
                "(SELECT COUNT(*) FROM router_evidence e "
                "WHERE e.ticket_id=t.ticket_id) AS evidence_count "
                "FROM router_tickets t JOIN router_threads h "
                "ON h.thread_id=t.thread_id "
                "WHERE h.owner=? AND h.room=? AND t.status='review' "
                "ORDER BY t.updated_at,t.created_at LIMIT ?",
                (owner, room, limit),
            )]

    def get_ticket(self, owner: str, room: str, ticket_id: str) -> dict:
        group_session(owner,room)
        with self._lock:
            row = dict(self._ticket(ticket_id,owner,room))
            row["evidence_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM router_evidence WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()[0]
            # Claim tokens are worker authority; never leak via read APIs.
            row.pop("claim_token",None)
            return row

