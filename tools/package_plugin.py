"""Build an install ZIP from an explicit source manifest; no plugin loading."""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_wechat_agents"
FILES = [
    "__init__.py",
    "_conf_schema.json",
    "workspace.py",
    "control_auth.py",
    "control_subscriptions.py",
    "control_tasks.py",
    "control_views.py",
    "control.py",
    "identity.py",
    "main.py",
    "metadata.yaml",
    "README.md",
    "router_core.py",
    "router_evidence.py",
    "router_subscriptions.py",
    "router_tasks.py",
    "router_threads.py",
    "router.py",
    "subscription_scheduler.py",
    "task_cli.py",
    "task_config.py",
    "task_panel.py",
    "task_runtime.py",
    "task_worker.py",
    "ticket_worker.py",
    "tool_gate.py",
    ".astrbot-plugin/i18n/zh-CN.json",
    "docs/TASK_UI.md",
    "views/tasks/app.js",
    "views/tasks/index.html",
    "views/tasks/style.css"
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / (PLUGIN_NAME + ".zip"))
    args = parser.parse_args()
    sources = [(name, (ROOT / name).read_bytes()) for name in FILES]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sources:
            archive.writestr(f"{PLUGIN_NAME}/{name}", content)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
