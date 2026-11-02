"""Public TaskRouter facade assembled from functional state-machine modules."""
from __future__ import annotations

from .router_core import (
    SubscriptionAssignment,
    TicketAssignment,
    _required,
)
from .router_tasks import TaskOperations
from .router_evidence import EvidenceOperations
from .router_subscriptions import SubscriptionOperations


class TaskRouter(TaskOperations, EvidenceOperations, SubscriptionOperations):
    """Persistent task router; storage and business operations remain separate."""


__all__ = [
    "TaskRouter",
    "TicketAssignment",
    "SubscriptionAssignment",
    "_required",
]
