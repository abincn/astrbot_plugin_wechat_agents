"""Scoped one-shot Subscription control operations."""
from __future__ import annotations

from .router import SubscriptionAssignment


class SubscriptionControlOperations:
    def schedule_followup(self, token: str, owner: str, room: str,
                          thread_id: str, client_key: str, task: str,
                          due_at: int) -> str:
        p = self._principal(token, owner, room, "subscription:manage")
        return self.router.schedule_subscription(
            owner, room, thread_id, client_key, task, due_at, actor=p.subject
        )

    def pause_followup(self, token: str, owner: str, room: str,
                       subscription_id: str) -> None:
        p = self._principal(token, owner, room, "subscription:manage")
        self.router.pause_subscription(
            owner, room, subscription_id, actor=p.subject
        )

    def resume_followup(self, token: str, owner: str, room: str,
                        subscription_id: str) -> None:
        p = self._principal(token, owner, room, "subscription:manage")
        self.router.resume_subscription(
            owner, room, subscription_id, actor=p.subject
        )

    def cancel_followup(self, token: str, owner: str, room: str,
                        subscription_id: str) -> None:
        p = self._principal(token, owner, room, "subscription:manage")
        self.router.cancel_subscription(
            owner, room, subscription_id, actor=p.subject
        )

    def claim_due_followups(self, token: str, owner: str, room: str, *,
                            limit: int = 10,
                            lease_seconds: int = 60) -> list[SubscriptionAssignment]:
        p = self._principal(token, owner, room, "subscription:dispatch")
        return self.router.claim_due_subscriptions(
            owner, room, limit=limit, lease_seconds=lease_seconds,
            worker="scheduler", actor=p.subject,
        )

    def fire_followup(self, token: str,
                      assignment: SubscriptionAssignment) -> str | None:
        if not isinstance(assignment, SubscriptionAssignment):
            raise PermissionError("A valid scheduler assignment is required")
        p = self._principal(
            token, assignment.owner, assignment.room, "subscription:dispatch"
        )
        return self.router.fire_subscription(assignment, actor=p.subject)

    def get_followup(self, token: str, owner: str, room: str,
                     subscription_id: str) -> dict:
        self._principal(token, owner, room, "subscription:read")
        return self.router.get_subscription(owner, room, subscription_id)
