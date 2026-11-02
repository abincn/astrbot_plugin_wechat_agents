"""AstrBot-native view bridge; Dashboard login plus existing scoped credential."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

from astrbot.api.web import error_response, json_response, request

from .control import TaskControlPlane
from .control_auth import ControlAuthenticator
from .control_views import task_snapshot, thread_detail
from .router import TaskRouter
from .task_cli import OPERATIONS
from .task_config import CONFIG_ENV, load_config, resolve_task_platform


PLUGIN_NAME = "astrbot_plugin_wechat_agents"


class TaskPanel:
    def __init__(self, context, group_settings: dict):
        self.context = context
        self.group_settings = group_settings
        self.active = True
        context.register_web_api(
            f"/{PLUGIN_NAME}/tasks", self.handle, ["POST"], "微信任务工作台",
        )

    async def handle(self):
        # AstrBot's extension route authenticates the Dashboard session first.
        if not self.active or not request.username:
            return error_response("请登录 AstrBot 并启用微信 Agent 任务插件。", status_code=403)
        if not self.group_settings["rooms"]:
            return error_response("请先在“微信 Agent 任务 → 配置”中添加群 ID，保存后重启。", status_code=503)
        path = os.environ.get(CONFIG_ENV)
        if not path:
            return error_response("尚未配置独立实例的任务配置文件。", status_code=503)
        try:
            raw = await request.body()
            if len(raw) > 65536:
                return error_response("提交内容过长，请缩短到 64 KiB 内。", status_code=413)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            config = load_config(path, group_settings=self.group_settings)
            # Resolve on the event loop, then do SQLite work off it.
            resolve_task_platform(self.context, config)
            data = await asyncio.to_thread(self._execute, config, payload)
            return json_response(data, headers={"Cache-Control": "no-store"})
        except PermissionError:
            return error_response("操作凭据无效，或没有当前群的操作权限。", status_code=403)
        except (ValueError, TypeError, KeyError):
            return error_response("操作未完成：请检查填写内容、任务状态及审核证据。")
        except (OSError, sqlite3.Error):
            return error_response("无法读取任务配置或数据库，请检查独立实例配置。", status_code=503)

    def _execute(self, config: dict, payload: dict) -> dict:
        # No bearer credentials in URL, disk, response, log or global state.
        token = payload.get("token")
        auth = ControlAuthenticator.from_file(config["grant_file"])
        principal = auth.authenticate(token)
        if principal.owner != config["owner"]:
            raise PermissionError("Wrong account")
        rooms = [room for room in config["rooms"] if room in principal.rooms]
        if not rooms:
            raise PermissionError("No configured rooms")
        operation = payload.get("operation")
        if operation == "connect":
            return {"subject": principal.subject, "owner": principal.owner,
                    "rooms": rooms, "capabilities": sorted(principal.capabilities)}
        room = payload.get("room")
        if room not in rooms:
            raise PermissionError("Wrong room")
        router = TaskRouter(config["inbox_db"])
        try:
            control = TaskControlPlane(router, auth)
            if operation == "snapshot":
                return task_snapshot(control, token, config["owner"], room)
            if operation == "thread_detail":
                thread_id = payload.get("thread_id")
                if not isinstance(thread_id, str):
                    raise ValueError("Missing Thread")
                return thread_detail(control, token, config["owner"], room, thread_id)
            if not isinstance(operation, str) or operation not in OPERATIONS:
                raise ValueError("Disallowed action")
            parameters = payload.get("parameters", {})
            if not isinstance(parameters, dict) or parameters.keys() & {
                "self", "token", "owner", "room",
            }:
                raise ValueError("Invalid operation parameters")
            result = getattr(control, operation)(
                token, config["owner"], room, **parameters,
            )
            return {"result": result}
        finally:
            router.close()
