"""Opt-in adapter-bound lifecycle for existing task workers."""
from __future__ import annotations

import asyncio
import os

from .task_config import (
    CONFIG_ENV, TICKET_TOKEN_ENV, SCHEDULER_TOKEN_ENV,
    load_config, open_control, resolve_task_platform,
)
from .subscription_scheduler import SubscriptionPoller
from .ticket_worker import AstrBotTicketWorker


class TaskRuntime:
    """One optional supervisor per standalone AstrBot plugin instance."""

    def __init__(self, context, config: dict):
        platform = resolve_task_platform(context, config)
        control = open_control(config)
        try:
            scopes = [(config["owner"], room) for room in config["rooms"]]
            self._workers = []
            if config.get("run_ticket_worker"):
                token = os.environ.get(TICKET_TOKEN_ENV, "")
                provider = config["provider_id"]
                self._workers.append(AstrBotTicketWorker(
                    control, token, context, provider, [provider], scopes,
                ))
            if config.get("run_subscription_worker"):
                token = os.environ.get(SCHEDULER_TOKEN_ENV, "")
                self._workers.append(SubscriptionPoller(
                    control, token, scopes,
                ))
            self.control = control
            self._platform = platform
            self._stop_event = asyncio.Event()
            self._tasks: list[asyncio.Task] = []
            self._ticket_task: asyncio.Task | None = None
            self._stop_lock = asyncio.Lock()
            self._closed = False
        except BaseException:
            control.router.close()
            raise

    @classmethod
    def from_environment(cls, context) -> "TaskRuntime | None":
        path = os.environ.get(CONFIG_ENV)
        if not path:
            return None
        config = load_config(path)
        if not config.get("run_ticket_worker") and not config.get(
            "run_subscription_worker"
        ):
            return None
        return cls(context, config)

    def start(self) -> None:
        if self._tasks or self._closed:
            raise RuntimeError("TaskRuntime already started")
        self._platform.attach_task_runtime(self)
        for worker in self._workers:
            task = asyncio.create_task(
                worker.run(self._stop_event),
                name="wechat_hook_" + type(worker).__name__,
            )
            self._tasks.append(task)
            if isinstance(worker, AstrBotTicketWorker):
                self._ticket_task = task

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._closed:
                return
            self._stop_event.set()
            if self._ticket_task is not None:
                # The existing model adapter records cancellation as review.
                self._ticket_task.cancel()
            # Never cancel the scheduler's to_thread transaction: let it finish
            # before closing SQLite, even when platform and plugin both stop.
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self.control.router.close()
            self._tasks.clear()
            self._platform.detach_task_runtime(self)
            self._closed = True
