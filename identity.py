"""Task scope validation without importing a sibling AstrBot plugin."""
from __future__ import annotations


def group_session(owner: str, room: str) -> str:
    if not owner or ":" in owner or "|" in owner or not room.endswith("@chatroom") or "|" in room or ":" in room:
        raise ValueError("Invalid WeChat group identity")
    return owner + "|" + room


