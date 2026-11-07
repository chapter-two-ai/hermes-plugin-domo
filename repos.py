"""The git repositories on this Mac, so runs can be grouped by repo and started in one.

Roots come from DOMO_REPO_ROOTS (colon-separated, in the root .env or the process environment);
by default the usual places people keep code. The scan is shallow and cached briefly: the app
asks often, and walking a home directory is not free.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_ROOTS = ("~/work", "~/Developer", "~/code", "~/projects", "~/src", "~/GitHub")
CACHE_SECONDS = 30
MAX_DEPTH = 2          # a root, and one level of folders inside it (~/work/client/repo)
MAX_REPOS = 300
_SKIP = {"node_modules", ".build", "build", "dist", "DerivedData", ".venv", "venv", "Pods", ".git"}

_cache: dict[tuple, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


def roots(env: dict[str, str] | None = None) -> list[Path]:
    raw = (env or {}).get("DOMO_REPO_ROOTS") or os.environ.get("DOMO_REPO_ROOTS") or ":".join(DEFAULT_ROOTS)
    return [Path(os.path.expanduser(p)).resolve() for p in raw.split(":") if p.strip()]


def repo_id(path: Path) -> str:
    """A stable, URL-safe id: the folder name and a short hash of the full path."""
    slug = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "repo"
    return f"{slug}-{hashlib.sha1(str(path).encode()).hexdigest()[:6]}"


def _git(path: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _remote_name(url: str) -> str:
    """git@github.com:jonathangus/domo.git and https://github.com/jonathangus/domo → jonathangus/domo."""
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def describe(path: Path) -> dict:
    when = _git(path, "log", "-1", "--format=%ct")
    remote = _git(path, "config", "--get", "remote.origin.url")
    return {
        "id": repo_id(path),
        "name": path.name,
        "path": str(path),
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        "remote": _remote_name(remote),
        "last_commit_at": float(when) if when.isdigit() else None,
        "last_commit": _git(path, "log", "-1", "--format=%s")[:120],
        "dirty": bool(_git(path, "status", "--porcelain", "--untracked-files=no")),
    }


def _walk(root: Path, depth: int, found: list[Path]) -> None:
    if len(found) >= MAX_REPOS or not root.is_dir():
        return
    if (root / ".git").exists():
        found.append(root)
        return
    if depth >= MAX_DEPTH:
        return
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in _SKIP and not p.name.startswith("."))
    except OSError:
        return
    for child in children:
        _walk(child, depth + 1, found)


def scan(env: dict[str, str] | None = None, *, fresh: bool = False, clock=time.monotonic) -> list[dict]:
    """Every repo under the roots, most recently committed first."""
    key = tuple(str(r) for r in roots(env))
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
    found: list[Path] = []
    for root in roots(env):
        _walk(root, 0, found)
    repos = sorted((describe(p) for p in dict.fromkeys(found)),
                   key=lambda r: r["last_commit_at"] or 0, reverse=True)
    with _lock:
        _cache[key] = (clock(), repos)
    return repos


def find(repo: str, env: dict[str, str] | None = None) -> dict | None:
    """A repo by id, path or folder name."""
    for r in scan(env):
        if repo in (r["id"], r["path"], r["name"]):
            return r
    return None
