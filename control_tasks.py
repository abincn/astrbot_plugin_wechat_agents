"""Scoped Thread, Ticket, evidence and draft control operations."""
from __future__ import annotations

from .router import TicketAssignment


class TaskControlOperations:
    def open_thread(self, token: str, owner: str, room: str,
                    trigger_event_id: str, goal: str) -> str:
        p = self._principal(token, owner, room, "thread:create")
        return self.router.open_thread(
            owner, room, trigger_event_id, goal, actor=p.subject
        )

    def enqueue_ticket(self, token: str, owner: str, room: str,
                       thread_id: str, client_key: str, task: str) -> str:
        p = self._principal(token, owner, room, "ticket:create")
        return self.router.enqueue_ticket(
            owner, room, thread_id, client_key, task, actor=p.subject
        )

    def claim_ticket(self, token: str, owner: str, room: str,
                     ticket_id: str) -> TicketAssignment:
        self._principal(token, owner, room, "ticket:dispatch")
        return self.router.claim_ticket(owner, room, ticket_id, worker="astrbot")

    def claim_next_ticket(self, token: str, owner: str,
                          room: str) -> TicketAssignment | None:
        p = self._principal(token, owner, room, "ticket:dispatch")
        return self.router.claim_next_ticket(owner, room, actor=p.subject)

    def evaluate_candidate(self, token: str, owner: str, room: str,
                           ticket_id: str, *, accepted: bool, reason: str) -> None:
        """Legacy compatibility API; new approval flows must use approve_candidate()."""
        p = self._principal(token, owner, room, "ticket:review")
        self.router.evaluate_candidate(
            owner, room, ticket_id, reviewer=p.subject,
            accepted=accepted, reason=reason,
        )

    def record_evidence(self, token: str, owner: str, room: str,
                        ticket_id: str, source_type: str, source_ref: str,
                        summary: str) -> str:
        p = self._principal(token, owner, room, "ticket:review")
        return self.router.record_evidence(
            owner, room, ticket_id, source_type, source_ref, summary,
            actor=p.subject,
        )

    def list_evidence(self, token: str, owner: str, room: str,
                      ticket_id: str) -> list[dict]:
        self._principal(token, owner, room, "ticket:read")
        return self.router.list_evidence(owner, room, ticket_id)

    def approve_candidate(self, token: str, owner: str, room: str,
                          ticket_id: str, *, reason: str) -> None:
        p = self._principal(token, owner, room, "ticket:review")
        self.router.approve_candidate(
            owner, room, ticket_id, reviewer=p.subject, reason=reason,
        )

    def create_ticket_draft(self, token: str, owner: str, room: str,
                            ticket_id: str) -> int:
        p = self._principal(token, owner, room, "ticket:review")
        return self.router.create_ticket_draft(
            owner, room, ticket_id, actor=p.subject,
        )

    def requeue_review(self, token: str, owner: str, room: str,
                       ticket_id: str, *, reason: str) -> None:
        p = self._principal(token, owner, room, "ticket:retry")
        self.router.requeue_review(
            owner, room, ticket_id, reviewer=p.subject, reason=reason
        )

    def complete_thread(self, token: str, owner: str, room: str,
                        thread_id: str) -> None:
        p = self._principal(token, owner, room, "thread:complete")
        self.router.complete_thread(owner, room, thread_id, reviewer=p.subject)

    def get_thread(self, token: str, owner: str, room: str,
                   thread_id: str) -> dict:
        self._principal(token, owner, room, "thread:read")
        return self.router.get_thread(owner, room, thread_id)

    def get_ticket(self, token: str, owner: str, room: str,
                   ticket_id: str) -> dict:
        self._principal(token, owner, room, "ticket:read")
        return self.router.get_ticket(owner, room, ticket_id)

    def list_review_tickets(self, token: str, owner: str, room: str,
                            *, limit: int = 50) -> list[dict]:
        self._principal(token, owner, room, "ticket:read")
        return self.router.list_review_tickets(owner, room, limit=limit)

    def audit(self, token: str, owner: str, room: str,
              thread_id: str) -> list[dict]:
        self._principal(token, owner, room, "audit:read")
        return self.router.audit(owner, room, thread_id)
