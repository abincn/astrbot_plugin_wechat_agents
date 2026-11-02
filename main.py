"""Group policy and tasks; no WeChat transport registration or Hook polling."""
from pathlib import Path
import asyncio
import logging

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import ToolSet
from astrbot.api.message_components import Reply, Video

from .task_config import normalize_group_settings
from .task_panel import TaskPanel
from .task_runtime import TaskRuntime
from .workspace import GroupWorkspace
from .video_frames import VideoFrameError, extract_video_frames


log = logging.getLogger(__name__)


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._group_settings = normalize_group_settings(config or {})
        self._task_runtime = None
        self._video_lock = asyncio.Lock()
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
        videos = [component for component in event.message_obj.message if isinstance(component, Video)]
        for component in event.message_obj.message:
            if isinstance(component, Reply):
                videos.extend(item for item in (component.chain or []) if isinstance(item, Video))
        if videos and self._group_settings["video_frames_enabled"]:
            try:
                platform = self.context.get_platform_inst(self._group_settings["platform_id"])
                scope = platform.get_task_scope()
                media_root = Path(scope["inbox_db"]).resolve().parent / "media"
                async with self._video_lock:
                    source = videos[0].path
                    if not source:
                        raise VideoFrameError("Video has no verified local path")
                    frames = await asyncio.to_thread(extract_video_frames, source, media_root, workspace.folder)
                req.image_urls = list(req.image_urls or [])
                for _, path in frames:
                    if path not in req.image_urls:
                        req.image_urls.append(path)
                times = ", ".join(str(second) for second, _ in frames)
                req.prompt += f"\n[视频抽帧依次约为 {times} 秒。只根据可见画面分析，不推断音轨或未见内容。]"
            except (VideoFrameError, OSError, ValueError, AttributeError) as exc:
                reason = str(exc) if isinstance(exc, VideoFrameError) else type(exc).__name__
                log.warning("WeChat video frames unavailable: %s", reason)
                req.prompt += "\n[视频已接收，但抽帧未就绪。请说明暂时无法查看画面，不要仅凭文件路径猜测内容。]"
        # Scope this pilot's no-tools policy to managed Agent groups only.
        req.func_tool = ToolSet()
