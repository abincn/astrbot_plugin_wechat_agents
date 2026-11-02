"""Local-only, authenticated control entrypoint for the existing task modules.

Usage:
    python -m data.plugins.astrbot_plugin_wechat_agents.task_cli --config PATH
Pass a single JSON action through stdin and set WGA_SUPERAGENT_OPERATOR_TOKEN
in the caller's environment. No web service, callback or chat command exists.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .control_auth import new_operator_credential
from .task_config import (
    CONFIG_ENV, OPERATOR_TOKEN_ENV, load_config, open_control,
)


# Only the human-facing operations are reachable here. Ticket claiming and
# Subscription dispatch remain reserved for dedicated scoped Workers.
OPERATIONS = frozenset({
    "open_thread", "enqueue_ticket", "get_thread", "get_ticket",
    "list_review_tickets", "record_evidence", "list_evidence",
    "approve_candidate", "create_ticket_draft", "complete_thread",
    "requeue_review", "audit", "schedule_followup", "get_followup",
    "pause_followup", "resume_followup", "cancel_followup",
})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authenticated local Thread/Ticket/Subscription control"
    )
    parser.add_argument("--config", default=os.environ.get(CONFIG_ENV))
    parser.add_argument(
        "--generate-credential", action="store_true",
        help="Print a new bearer token and its hash for a local grant file",
    )
    args = parser.parse_args()
    if args.generate_credential:
        token, digest = new_operator_credential()
        print(json.dumps({"bearer_token": token, "token_sha256": digest}))
        return 0

    if not args.config:
        parser.error("--config or WGA_SUPERAGENT_CONFIG is required")
    token = os.environ.get(OPERATOR_TOKEN_ENV)
    if not token:
        parser.error(f"{OPERATOR_TOKEN_ENV} must be configured")

    raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        parser.error("Action JSON exceeds 64 KiB")
    action = json.loads(raw)
    if not isinstance(action, dict):
        parser.error("Action JSON must be an object")
    operation = action.pop("action", None)
    if operation not in OPERATIONS:
        parser.error("Unknown or disallowed action")
    config = load_config(args.config)
    if action.get("owner") != config["owner"] or action.get("room") not in config["rooms"]:
        parser.error("Action scope is not in the configured account/group allowlist")

    control = open_control(config)
    try:
        result = getattr(control, operation)(token, **action)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    finally:
        control.router.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, TypeError, PermissionError, OSError) as exc:
        # Avoid printing task text, raw credential values or provider payloads.
        print("Task control failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(2)
