"""Opt-in polling loop for durable one-shot Subscription wake-ups.

The poller only converts due Subscriptions into queued Tickets. It does not
invoke AstrBot, run models, execute tools, create Outbox drafts, or send WeChat
messages. The host must explicitly start and supervise run(); importing this
module has no side effects.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from .identity import group_session
from .control import TaskControlPlane


class SubscriptionPoller:
    def __init__(
        self,
        control: TaskControlPlane,
        scheduler_token: str,
        scopes: Sequence[tuple[str, str]],
        *,
        interval_seconds: float = 2.0,
        batch_size: int = 10,
        lease_seconds: int = 60,
    ):
        if not isinstance(scheduler_token, str) or len(scheduler_token) < 32:
            raise ValueError("A high-entropy scheduler token is required")
        if not isinstance(scopes, (list, tuple)) or not scopes:
            raise ValueError("At least one exact account/group scope is required")
        normalized = []
        for owner, room in scopes:
            group_session(owner, room)
            scope = (owner, room)
            if scope in normalized:
                raise ValueError("Duplicate scheduler scope")
            normalized.append(scope)
        if type(interval_seconds) not in (int, float) or not 0.5 <= interval_seconds <= 300:
            raise ValueError("interval_seconds must be between 0.5 and 300")
        if type(batch_size) is not int or not 1 <= batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 10 and 900")
        principal = control.authenticator.authenticate(scheduler_token)
        for owner, room in normalized:
            control.authenticator.authorize(
                principal, owner, room, "subscription:dispatch"
            )
        self.control = control
        self._token = scheduler_token
        self.scopes = tuple(normalized)
        self.interval_seconds = float(interval_seconds)
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self._logger = logging.getLogger(__name__)

    def run_once(self) -> dict[str, int]:
        """Claim and atomically fire due wake-ups across configured exact scopes."""
        totals = {"claimed": 0, "fired": 0, "review": 0, "stale": 0}
        for owner, room in self.scopes:
            assignments = self.control.claim_due_followups(
                self._token, owner, room,
                limit=self.batch_size, lease_seconds=self.lease_seconds,
            )
            totals["claimed"] += len(assignments)
            for assignment in assignments:
                try:
                    ticket_id = self.control.fire_followup(self._token, assignment)
                except ValueError:
                    # Lease expired, was cancelled/paused, or was reclaimed.
                    totals["stale"] += 1
                    continue
                if ticket_id is None:
                    totals["review"] += 1
                else:
                    totals["fired"] += 1
        return totals

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run until the host sets stop_event; no implicit plugin startup."""
        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be an asyncio.Event")
        while not stop_event.is_set():
            try:
                # SQLite work is synchronous; keep it off AstrBot's event loop.
                await asyncio.to_thread(self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log credentials, exception payloads, or task contents.
                self._logger.error(
                    "Subscription poll iteration failed (%s)",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass
