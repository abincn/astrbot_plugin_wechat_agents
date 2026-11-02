"""Opt-in, sequential AstrBot Ticket dispatcher.

This worker polls only explicitly queued Tickets in configured exact groups.
It uses the native no-tools task_worker adapter and an explicit provider
allowlist. Model output is always a candidate requiring independent Eval.
No tools, MCP, external actions, Outbox drafts or WeChat sends are enabled.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from .identity import group_session
from .control import TaskControlPlane
from .task_worker import execute_assignment


class AstrBotTicketWorker:
    def __init__(
        self,
        control: TaskControlPlane,
        worker_token: str,
        context,
        provider_id: str,
        allowed_provider_ids: Sequence[str],
        scopes: Sequence[tuple[str, str]],
        *,
        timeout_seconds: float = 30.0,
        idle_interval_seconds: float = 2.0,
    ):
        if not isinstance(worker_token, str) or len(worker_token) < 32:
            raise ValueError("A high-entropy Ticket worker token is required")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be non-empty text")
        if not isinstance(allowed_provider_ids, (list, tuple, set, frozenset)):
            raise ValueError("allowed_provider_ids must be an explicit collection")
        allowed = frozenset(
            item.strip() for item in allowed_provider_ids
            if isinstance(item, str) and item.strip()
        )
        if not allowed or provider_id not in allowed:
            raise ValueError("provider_id is not in the configured provider allowlist")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if (type(idle_interval_seconds) not in (int, float)
                or not 0.5 <= idle_interval_seconds <= 300):
            raise ValueError("idle_interval_seconds must be between 0.5 and 300")
        if not isinstance(scopes, (list, tuple)) or not scopes:
            raise ValueError("At least one exact account/group scope is required")
        normalized = []
        for owner, room in scopes:
            group_session(owner, room)
            scope = (owner, room)
            if scope in normalized:
                raise ValueError("Duplicate Ticket worker scope")
            normalized.append(scope)

        principal = control.authenticator.authenticate(worker_token)
        for owner, room in normalized:
            control.authenticator.authorize(
                principal, owner, room, "ticket:dispatch"
            )
        self.control = control
        self._token = worker_token
        self.context = context
        self.provider_id = provider_id.strip()
        self.scopes = tuple(normalized)
        self.timeout_seconds = float(timeout_seconds)
        self.idle_interval_seconds = float(idle_interval_seconds)
        self._logger = logging.getLogger(__name__)

    async def run_once(self) -> dict[str, int]:
        """Claim at most one Ticket and run one bounded no-tools model call."""
        for owner, room in self.scopes:
            assignment = self.control.claim_next_ticket(
                self._token, owner, room
            )
            if assignment is None:
                continue
            try:
                await execute_assignment(
                    self.control.router,
                    assignment,
                    context=self.context,
                    provider_id=self.provider_id,
                    timeout_seconds=self.timeout_seconds,
                )
                return {"claimed": 1, "candidate": 1, "failed": 0}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # task_worker records a failure category and moves the Ticket
                # to review. Never log task text, provider payloads or tokens.
                self._logger.error(
                    "AstrBot Ticket execution failed (%s)",
                    type(exc).__name__,
                )
                return {"claimed": 1, "candidate": 0, "failed": 1}
        return {"claimed": 0, "candidate": 0, "failed": 0}

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run sequentially until stopped; the plugin never starts this implicitly."""
        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be an asyncio.Event")
        while not stop_event.is_set():
            try:
                stats = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.error(
                    "AstrBot Ticket poll iteration failed (%s)",
                    type(exc).__name__,
                )
                stats = {"claimed": 0}
            if stats.get("claimed", 0):
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.idle_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
