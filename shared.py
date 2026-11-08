"""Shared notes: plain markdown that Jonathan's and Frida's agents both read and write.

Hermes keeps memory per profile and warns against two agents sharing one memory file, so what the
couple shares (apartment hunt, daughter, trips, home) lives here instead: one file per topic in
``<hermes root>/domo-shared/``, readable and editable by hand. Only profiles whose
``plugin-data/domo/profile.json`` says ``"shared_notes": true`` get the tool; Family never does.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

MAX_READ = 20_000
MAX_NOTE = 200_000
_SLUG = re.compile(r"[^a-z0-9]+")


class SharedError(ValueError):
    pass


def hermes_root_of(home: Path) -> Path:
    home = Path(home)
    return home.parent.parent if home.parent.name == "profiles" else home


def shared_dir(home: Path) -> Path:
    override = os.environ.get("DOMO_SHARED_DIR")
    return Path(override) if override else hermes_root_of(home) / "domo-shared"


def profile_json(home: Path) -> dict:
    path = Path(home) / "plugin-data" / "domo" / "profile.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def enabled(home: Path) -> bool:
    return bool(profile_json(home).get("shared_notes"))


def author(home: Path) -> str:
    info = profile_json(home)
    return info.get("display_name") or ("Family" if hermes_root_of(home) == Path(home) else Path(home).name.title())


def slug(topic: str) -> str:
    s = _SLUG.sub("-", (topic or "").strip().lower()).strip("-")[:60]
    if not s or s == "readme":
        raise SharedError("give a topic, e.g. 'Apartment hunt'")
    return s


def note_path(home: Path, topic: str) -> Path:
    return shared_dir(home) / f"{slug(topic)}.md"


def topics(home: Path) -> list[dict]:
    d = shared_dir(home)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        first = next((l.lstrip("# ").strip() for l in p.read_text().splitlines() if l.strip()), p.stem)
        out.append({"topic": p.stem, "title": first, "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                    "chars": p.stat().st_size})
    return out


def read(home: Path, topic: str) -> str:
    p = note_path(home, topic)
    if not p.exists():
        return ""
    text = p.read_text()
    return text if len(text) <= MAX_READ else "…(start trimmed)\n" + text[-MAX_READ:]


def _write(p: Path, text: str) -> None:
    if len(text) > MAX_NOTE:
        raise SharedError(f"note would be over {MAX_NOTE} characters; rewrite it shorter with action 'write'")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, p)


def append(home: Path, topic: str, text: str, *, now: float | None = None) -> str:
    text = (text or "").strip()
    if not text:
        raise SharedError("nothing to add")
    p = note_path(home, topic)
    existing = p.read_text() if p.exists() else f"# {topic.strip()}\n"
    stamp = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
    entry = f"- {stamp} ({author(home)}): " + text.replace("\n", "\n  ")
    _write(p, existing.rstrip("\n") + "\n" + entry + "\n")
    return entry


def write(home: Path, topic: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        raise SharedError("refusing to empty a shared note; append instead, or ask the person first")
    if not text.startswith("#"):
        text = f"# {topic.strip()}\n\n{text}"
    _write(note_path(home, topic), text + "\n")


README = """# Domo shared notes

Jonathan's and Frida's Domo agents both read and write these notes: one markdown file per topic
(apartment hunt, daughter, trips, home). Each agent keeps its own private memory; anything the two of
you share goes here instead. Edit them freely: the agents read them fresh every time.
"""
