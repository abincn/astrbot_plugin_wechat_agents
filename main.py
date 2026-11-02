"""Group policy and tasks; no WeChat transport registration or Hook polling."""
from pathlib import Path

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import ToolSet

from .task_config import normalize_group_settings
from .task_panel import TaskPanel
from .task_runtime import TaskRuntime
from .workspace import GroupWorkspace


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._group_settings = normalize_group_settings(config or {})
        self._task_runtime = None
        self._task_panel = TaskPanel(context, self._group_settings)

    @filter.on_astrbot_loaded()
    async def start_task_runtime(self) -> None:
        if self._task_runtime is not None:
            return
        runtime = TaskRuntime.from_environment(self.context, self._group_settings)
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

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        if (event.get_platform_name() != "wechat_pc_hook"
                or event.get_platform_id() != self._group_settings["platform_id"]
                or event.get_group_id() not in self._group_settings["rooms"]):
            return
        namespace = event.get_extra("wechat_workspace_id")
        if not namespace:
            raise RuntimeError("Missing per-group namespace")
        workspace = GroupWorkspace(Path("data/wechat_hook_workspaces"), namespace)
        instructions = workspace.instructions()
        if instructions:
            req.system_prompt += "\n\n[Current WeChat group instructions]\n" + instructions
        # Scope this pilot's no-tools policy to managed Agent groups only.
        req.func_tool = ToolSet()
