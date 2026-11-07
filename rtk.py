"""Terminal output through RTK: the agent's shell commands are rewritten to their `rtk` form
(`git status` → `rtk git status`), so what comes back is compact (failures only, grouped
matches, short logs) instead of thousands of lines of context.

RTK ships its own Hermes plugin, but it edits the tool arguments in place; Hermes 0.23 hands
hooks a copy and only applies a returned ``{"action": "modify"}`` directive, so its rewrites are
lost. This hook asks the same `rtk rewrite` and returns the directive. It fails open: any
problem leaves the command as it was.
"""

from __future__ import annotations

import os
import shutil
import subprocess

TIMEOUT = 5
REWRITTEN = {0, 3}      # rtk rewrite: 0 rewritten, 3 rewritten with a note; 1/2 mean leave it


# Services (launchd on the Mac, Hermes's own supervisor) start with a short PATH, so Homebrew's
# rtk isn't found by name: setup records its full path, and the usual places are tried after.
CANDIDATES = ("/opt/homebrew/bin/rtk", "/usr/local/bin/rtk", "~/.local/bin/rtk", "~/.cargo/bin/rtk")


def binary() -> str | None:
    if os.environ.get("DOMO_RTK", "on") == "off":
        return None
    recorded = os.environ.get("DOMO_RTK_BIN")
    if not recorded:
        try:
            from . import bridge
            recorded = bridge.read_env(bridge.hermes_root()).get("DOMO_RTK_BIN")
        except Exception:
            recorded = None
    for path in filter(None, (recorded, shutil.which("rtk"), *CANDIDATES)):
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def available() -> bool:
    return binary() is not None


def rewrite(command: str) -> str | None:
    exe = binary()
    if not exe:
        return None
    try:
        result = subprocess.run([exe, "rewrite", command], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in REWRITTEN:
        return None
    out = result.stdout.strip()
    return out if out and out != command else None


def pre_tool_call(tool_name=None, args=None, **_):
    try:
        if tool_name != "terminal" or not isinstance(args, dict):
            return None
        command = args.get("command")
        if not isinstance(command, str) or not command.strip() or command.lstrip().startswith("rtk "):
            return None
        new = rewrite(command)
        return {"action": "modify", "args": {"command": new}} if new else None
    except Exception:
        return None
