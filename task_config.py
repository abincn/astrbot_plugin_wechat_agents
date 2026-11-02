"""Shared minimal configuration and authenticated Router construction.

The local control CLI imports this module without importing any worker,
AstrBot runtime, or network stack.
"""
from __future__ import annotations

import json
from pathlib import Path

from .control import TaskControlPlane
from .control_auth import ControlAuthenticator
from .identity import group_session
from .router import TaskRouter


CONFIG_ENV = "WGA_SUPERAGENT_CONFIG"
TICKET_TOKEN_ENV = "WGA_SUPERAGENT_TICKET_TOKEN"
SCHEDULER_TOKEN_ENV = "WGA_SUPERAGENT_SCHEDULER_TOKEN"
OPERATOR_TOKEN_ENV = "WGA_SUPERAGENT_OPERATOR_TOKEN"


def normalize_group_settings(settings: dict) -> dict:
    """The Agent plugin config is the only authority for managed groups."""
    platform_id = settings.get("platform_id", "wechat_pc_hook")
    if not isinstance(platform_id, str) or not platform_id.strip():
        raise ValueError("Agent platform_id must be non-empty text")
    rooms = settings.get("rooms", [])
    if not isinstance(rooms, list) or len(rooms) > 50:
        raise ValueError("Agent rooms must be a list of at most 50 group IDs")
    normalized = []
    for room in rooms:
        if not isinstance(room, str):
            raise ValueError("Agent room must be text")
        room = room.strip()
        group_session("scope", room)
        if room in normalized:
            raise ValueError("Duplicate Agent room")
        normalized.append(room)
    return {"platform_id": platform_id.strip(), "rooms": normalized}


def load_group_settings(path: str | Path) -> dict:
    location = Path(path).expanduser()
    if location.stat().st_size > 65536:
        raise ValueError("Agent plugin config must be at most 64 KiB")
    settings = json.loads(location.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("Agent plugin config must be an object")
    return normalize_group_settings(settings)


def load_config(path: str | Path, *, group_settings: dict) -> dict:
    """Read one bounded, local configuration shared by workers and control CLI."""
    config_path = Path(path).expanduser().resolve(strict=True)
    if config_path.stat().st_size > 8192:
        raise ValueError("Super-Agent config must be at most 8 KiB")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Super-Agent config must be a JSON object")
    expected = {
        "inbox_db", "grant_file", "owner", "rooms", "provider_id",
        "run_ticket_worker", "run_subscription_worker", "platform_id",
    }
    if set(config) - expected:
        raise ValueError("Unknown Super-Agent config fields")
    # Legacy rooms/platform_id in this file are ignored, never a fallback.
    config.update(normalize_group_settings(group_settings))
    for key in ("inbox_db", "grant_file", "owner"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"Missing Super-Agent config field: {key}")
    platform_id = config.get("platform_id", "wechat_pc_hook")
    if not isinstance(platform_id, str) or not platform_id.strip():
        raise ValueError("platform_id must be non-empty text")
    config["platform_id"] = platform_id
    rooms = config.get("rooms")
    if not isinstance(rooms, list) or not rooms or len(rooms) > 50:
        raise ValueError("rooms must contain 1–50 exact group IDs")
    if any(not isinstance(room, str) for room in rooms):
        raise ValueError("rooms must contain only group ID strings")
    if len(rooms) != len(set(rooms)):
        raise ValueError("Duplicate Super-Agent room")
    for room in rooms:
        group_session(config["owner"], room)
    for flag in ("run_ticket_worker", "run_subscription_worker"):
        if type(config.get(flag, False)) is not bool:
            raise ValueError(f"{flag} must be a boolean")
    if config.get("run_ticket_worker") and (
        not isinstance(config.get("provider_id"), str)
        or not config["provider_id"].strip()
    ):
        raise ValueError("provider_id required when Ticket Worker is enabled")
    # Paths are anchored to the config directory, never to a process-dependent CWD.
    for key in ("inbox_db", "grant_file"):
        target = Path(config[key]).expanduser()
        if not target.is_absolute():
            target = config_path.parent / target
        config[key] = str(target.resolve(strict=True))
    config["rooms"] = tuple(rooms)
    return config


def open_control(config: dict) -> TaskControlPlane:
    router = TaskRouter(config["inbox_db"])
    try:
        auth = ControlAuthenticator.from_file(config["grant_file"])
        return TaskControlPlane(router, auth)
    except BaseException:
        router.close()
        raise


def resolve_task_platform(context, config: dict):
    """Bind both workers and Dashboard control to the isolated adapter."""
    platform = context.get_platform_inst(config.get("platform_id", "wechat_pc_hook"))
    if (platform is None or platform.meta().name != "wechat_pc_hook"
            or not callable(getattr(platform, "get_task_scope", None))
            or not callable(getattr(platform, "attach_task_runtime", None))
            or not callable(getattr(platform, "detach_task_runtime", None))):
        raise ValueError("Install and enable the WeChat Hook connector v0.3 or later")
    scope = platform.get_task_scope()
    if scope.get("version") != 2 or scope.get("draft_only") is not True:
        raise ValueError("Task control requires a live draft-only adapter")
    if (scope["owner"] != config["owner"]
            or Path(config["inbox_db"]).resolve() != Path(scope["inbox_db"]).resolve()):
        raise ValueError("Task database/account must match the adapter")
    return platform
