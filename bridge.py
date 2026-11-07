"""Where each profile lives on disk and how to reach its API server.

The dashboard process serves every profile, so routes resolve the profile from the request and
talk to that profile's API server over loopback with the profile's own key.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT = "default"


class ProfileError(ValueError):
    pass


def hermes_root() -> Path:
    override = os.environ.get("DOMO_HERMES_ROOT")
    if override:
        return Path(override)
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def profile_home(name: str, root: Optional[Path] = None) -> Path:
    root = root or hermes_root()
    name = (name or DEFAULT).strip().lower()
    if name == DEFAULT:
        return root
    if not _NAME.fullmatch(name):
        raise ProfileError(f"bad profile name {name!r}")
    home = root / "profiles" / name
    if not (home / "config.yaml").exists():
        raise ProfileError(f"no profile {name!r}")
    return home


def profile_names(root: Optional[Path] = None) -> list[str]:
    root = root or hermes_root()
    names = [DEFAULT] if (root / "config.yaml").exists() else []
    profiles = root / "profiles"
    if profiles.is_dir():
        names += sorted(p.name for p in profiles.iterdir() if (p / "config.yaml").exists() and _NAME.fullmatch(p.name))
    return names


def read_env(home: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    path = home / ".env"
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def profile_info(name: str, root: Optional[Path] = None) -> dict:
    """Display details the setup script writes to plugin-data/domo/profile.json."""
    home = profile_home(name, root)
    path = home / "plugin-data" / "domo" / "profile.json"
    info = json.loads(path.read_text()) if path.exists() else {}
    return {"id": name, "display_name": info.get("display_name") or name.title(),
            "kind": info.get("kind", "person"), "members": info.get("members", []),
            "spaces": info.get("spaces", [])}


class ApiServer:
    """A profile's API server, reached over loopback through the multiplexed listener."""

    def __init__(self, profile: str, root: Optional[Path] = None, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.profile = (profile or DEFAULT).lower()
        root = root or hermes_root()
        home = profile_home(self.profile, root)
        root_env, own_env = read_env(root), read_env(home)
        self.key = own_env.get("API_SERVER_KEY", "")
        base = os.environ.get("DOMO_API_SERVER_URL") or f"http://127.0.0.1:{root_env.get('API_SERVER_PORT', '8642')}"
        self.prefix = "" if self.profile == DEFAULT else f"/p/{self.profile}"
        self.base = base.rstrip("/")
        self._transport = transport

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        if not self.key:
            raise ProfileError(f"profile {self.profile!r} has no API_SERVER_KEY")
        return httpx.AsyncClient(base_url=self.base + self.prefix, timeout=timeout, transport=self._transport,
                                 headers={"Authorization": f"Bearer {self.key}"})

    async def request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                      params: Optional[dict] = None, timeout: float = 30.0) -> httpx.Response:
        async with self._client(timeout) as client:
            return await client.request(method, path, json=json_body, params=params)

    async def stream(self, path: str, json_body: dict) -> AsyncIterator[bytes]:
        async with self._client(timeout=None) as client:
            async with client.stream("POST", path, json=json_body,
                                     headers={"Accept": "text/event-stream"}) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    yield _sse("error", {"status": response.status_code, "detail": body.decode(errors="replace")[:500]})
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
