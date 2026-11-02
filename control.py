"""Authenticated, scope-limited facade for trusted local task control."""
from __future__ import annotations

from .control_auth import (
    CAPABILITIES,
    ControlAuthenticator,
    ControlPrincipal,
    _Grant,
    _required,
    new_operator_credential,
)
from .router import SubscriptionAssignment, TaskRouter, TicketAssignment
from .control_tasks import TaskControlOperations
from .control_subscriptions import SubscriptionControlOperations


class TaskControlPlane(TaskControlOperations, SubscriptionControlOperations):
    """Authenticated facade; raw TaskRouter remains an internal storage API."""

    def __init__(self, router: TaskRouter, authenticator: ControlAuthenticator):
        self.router = router
        self.authenticator = authenticator

    def _principal(self, token: str, owner: str, room: str,
                   capability: str) -> ControlPrincipal:
        principal = self.authenticator.authenticate(token)
        self.authenticator.authorize(principal, owner, room, capability)
        return principal
