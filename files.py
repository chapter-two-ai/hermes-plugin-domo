"""Files people attach in the app, and files agents hand back.

An attachment is saved on the Mac first, so any kind of file works: the agent is told where it is
and opens it with its own tools. Images also travel inline to the model (Hermes takes data:image
URLs, not documents). Files an agent returns as ``MEDIA:<path>`` are served back to the app after
Hermes's own delivery check, the same one that guards sending them to WhatsApp.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import time
import uuid
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024           # per attachment
INLINE_IMAGE_BYTES = 5 * 1024 * 1024   # Hermes skips larger data URLs too
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MARK = "[Domo attachments]"            # the app reads this block back out of the history


def files_dir(home: Path) -> Path:
    return home / "plugin-data" / "domo" / "files"


def _safe_name(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" .") or "file"
    return name[:120]


def save(home: Path, name: str, data: bytes, mime: str = "", *, store=None, space_id: str | None = None,
         session_id: str | None = None) -> dict:
    """Write an upload under the profile's files folder; with a store, also record it (and its space)."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name} is {len(data) // (1024 * 1024)} MB; the limit is {MAX_BYTES // (1024 * 1024)} MB")
    safe = _safe_name(name)
    mime = mime or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    day = time.strftime("%Y-%m-%d")
    folder = files_dir(home) / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex[:8]}-{safe}"
    path.write_bytes(data)
    saved = {"id": path.stem, "name": safe, "mime": mime, "size": len(data), "path": str(path)}
    if store is None:
        return saved
    return {**store.add_file(str(path), safe, mime, len(data), space_id=space_id, session_id=session_id), **saved}


def describe(home: Path, path: str) -> dict | None:
    """An uploaded attachment by path; only files under this profile's files folder count."""
    p = Path(path).resolve()
    root = files_dir(home).resolve()
    if not p.is_file() or root not in p.parents:
        return None
    name = p.name.split("-", 1)[1] if "-" in p.name else p.name
    return {"id": p.stem, "name": name, "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": p.stat().st_size, "path": str(p)}


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def compose(text: str, attachments: list[dict]):
    """The message Hermes receives: the text, a list of what's attached and where, and small
    images inline as vision parts. A plain string when there is nothing to show the model."""
    if not attachments:
        return text
    lines = [MARK] + [f"- {a['name']} | {a['mime']} | {_human(a['size'])} | {a['path']}" for a in attachments]
    note = ("These files are saved on this Mac at the paths above; open them with your tools "
            "(read_file, terminal, vision) as needed.")
    body = (text.strip() + "\n\n" if text.strip() else "") + "\n".join(lines) + "\n" + note
    images = [a for a in attachments if a["mime"] in INLINE_TYPES and a["size"] <= INLINE_IMAGE_BYTES]
    if not images:
        return body
    parts: list[dict] = [{"type": "text", "text": body}]
    for a in images:
        data = base64.b64encode(Path(a["path"]).read_bytes()).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:{a['mime']};base64,{data}"}})
    return parts


def deliverable(path: str, home: Path) -> Path | None:
    """A file the app may download: an attachment of this profile, or a path Hermes itself would
    deliver as media (its denylist keeps credentials and system files out)."""
    if describe(home, path):
        return Path(path).resolve()
    try:
        from gateway.platforms.base import validate_media_delivery_path
    except Exception:  # outside Hermes (tests): attachments only
        return None
    safe = validate_media_delivery_path(path)
    return Path(safe) if safe else None
