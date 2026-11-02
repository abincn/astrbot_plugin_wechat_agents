"""Agent-only AstrBot entrypoint; does not register or poll a WeChat platform."""
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .task_panel import TaskPanel
from .task_runtime import TaskRuntime


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._task_runtime = None
        self._task_panel = TaskPanel(context)

    @filter.on_astrbot_loaded()
    async def start_task_runtime(self) -> None:
        if self._task_runtime is not None:
            return
        runtime = TaskRuntime.from_environment(self.context)
        if runtime is not None:
            try:
                runtime.start()
            except BaseException:
                await runtime.stop()
                raise
            self._task_runtime = runtime

    async def terminate(self) -> None:
        self._task_panel.active = False
        if self._task_runtime is not None:
            await self._task_runtime.stop()
            self._task_runtime = None
