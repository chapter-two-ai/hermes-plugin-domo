"""The stitcher: an internal job, not a routine (nobody edits or pauses it), that keeps the spaces in shape as
everything grows, so the system stays tidy with time instead of bloating.

Every half hour, for each agent, cheaply (Jev decides; code does the rest; no model is called):

1. tag what's new: chats nobody tagged yet, or with something new said (filing.sweep);
2. let things follow their chat: a file sent in a chat, a to-do or decision asked in it, get its tags;
3. merge duplicates: topics with near names (Lissabon, Lisbon, Lissabonresan) are put to Jev as "the same
   thing?"; the smaller one folds into the larger, its name kept as an alias for next time;
4. give each topic its home area: the area most of its chats are in (how topics group under areas);
5. let the quiet ones fade: a topic with one chat and nothing for a month leaves the lists (not deleted;
   a new chat about it brings it back).

What the person set is never undone: their tags, their removals, the topics they made.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from . import filing

log = logging.getLogger("domo.stitch")

EVERY = 30 * 60
FIRST = 90
QUIET_AFTER = 30 * 86400
SAME_MIN = 0.85
_thread: threading.Thread | None = None
_wake = threading.Event()
_guard = threading.Lock()
last: dict[str, dict] = {}


def _plain(name: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", name.lower()) if c.isalnum())


def similar(a: str, b: str) -> bool:
    """Names worth asking about: one contains the other, or they're mostly the same letters."""
    x, y = _plain(a), _plain(b)
    if not x or not y or x == y:
        return x == y and bool(x)
    if (x.startswith(y) or y.startswith(x)) and min(len(x), len(y)) >= 4:
        return True
    # short names differ by a letter and are still different things (Domo, Dom)
    return min(len(x), len(y)) >= 5 and SequenceMatcher(None, x, y).ratio() >= 0.75


def inherit(store) -> int:
    """Files and items take their chat's tags (the person's removals still stand)."""
    chats = store.tag_map("session")
    n = 0
    for f in store.files(limit=500):
        for t in chats.get(f.get("session_id") or "", []):
            if not any(x["space_id"] == t["space_id"] for x in store.tags("file", f["path"], include_off=True)):
                n += store.tag("file", f["path"], t["space_id"], by="domo", confidence=t["confidence"], reason="sent in a chat there")
    for kind in ("todo", "decision", "receipt", "run"):
        for item in store.list(kind, limit=200):
            for t in chats.get(item.get("session_id") or "", []):
                if not any(x["space_id"] == t["space_id"] for x in store.tags("item", item["id"], include_off=True)):
                    n += store.tag("item", item["id"], t["space_id"], by="domo", confidence=t["confidence"], reason="from a chat there")
    return n


def _asked(store) -> set[str]:
    row = store._db.execute("SELECT value FROM meta WHERE key = 'merge_asked'").fetchone()
    return set(json.loads(row[0])) if row else set()


def merge_duplicates(store) -> list[tuple[str, str]]:
    """Ask Jev about pairs of Domo's topics with near names; fold the surely-same ones together."""
    topics = [t for t in store.spaces() if t["kind"] == "topic"]
    asked = _asked(store)
    pairs = []
    for i, a in enumerate(topics):
        for b in topics[i + 1:]:
            key = "|".join(sorted((a["id"], b["id"])))
            if key in asked or "person" == a["origin"] == b["origin"]:
                continue   # two topics the person made are theirs to merge
            if any(similar(x, y) for x in (a["name"], *store.aliases(a["id"])) for y in (b["name"], *store.aliases(b["id"]))):
                pairs.append((a, b, key))
    if not pairs:
        return []
    pairs = pairs[:10]
    def about(t):
        titles = [s for s in store.tagged(t["id"], "session")[:3]]
        return f"“{t['name']}”" + (f" ({t['description']})" if t["description"] else "") + (f", in {len(titles)} chats" if titles else "")
    questions = {f"p{i}": {"type": "noul", "instructions": f"In this person's chats, are {about(a)} and {about(b)} the same thing, under two names?"}
                 for i, (a, b, _) in enumerate(pairs)}
    answers = filing.ask("Topics Domo keeps for one person's chats: projects, clients, people, places, trips and repos.", questions)
    merged = []
    for i, (a, b, key) in enumerate(pairs):
        asked.add(key)
        if float((answers.get(f"p{i}") or {}).get("noul") or 0) >= SAME_MIN and store.space(a["id"]) and store.space(b["id"]):
            keep, fold = sorted((a, b), key=lambda t: (t["origin"] == "person", t["counts"]["chats"]), reverse=True)
            store.merge_spaces(fold["id"], keep["id"])
            store.note("merged", f"“{fold['name']}” and “{keep['name']}” are one topic now", space=keep["name"])
            merged.append((fold["name"], keep["name"]))
    with store._tx() as db:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('merge_asked', ?)", (json.dumps(sorted(asked)),))
    return merged


def home_areas(store) -> int:
    """A topic's home area: where most of its chats are. Grouping only; the chats keep every tag."""
    chats = store.tag_map("session")
    changed = 0
    for t in [s for s in store.spaces() if s["kind"] == "topic"]:
        votes: dict[str, int] = {}
        for sid in store.tagged(t["id"], "session"):
            for x in chats.get(sid, []):
                if x["kind"] == "area":
                    votes[x["space_id"]] = votes.get(x["space_id"], 0) + 1
        best = max(votes, key=votes.get) if votes else None
        if best and best != t.get("parent_id"):
            store.set_space_meta(t["id"], parent_id=best)
            changed += 1
    return changed


def fade(store, now: float | None = None) -> int:
    """Quiet topics leave the lists: one chat or none, and nothing new for a month."""
    now = now or time.time()
    n = 0
    for t in [s for s in store.spaces() if s["kind"] == "topic" and not s["quiet"]]:
        busy = t["counts"]["chats"] + t["counts"]["files"] + t["counts"]["routines"]
        if busy <= 1 and now - (t["last_active"] or t["updated_at"] or now) > QUIET_AFTER:
            store.set_space_meta(t["id"], quiet=True)
            n += 1
    return n


def run_once(home: Path, store) -> dict:
    out = {"at": time.time(), "tagged": 0, "inherited": 0, "merged": [], "homes": 0, "faded": 0}
    if filing.jev_key():
        try:
            out["tagged"] = len(filing.sweep(home, store))
            out["merged"] = merge_duplicates(store)
        except filing.FilingError as exc:
            out["error"] = str(exc)
    out["inherited"] = inherit(store)
    out["homes"] = home_areas(store)
    out["faded"] = fade(store)
    return out


def ensure_running(profiles: Callable[[], list[str]], home_for: Callable[[str], Path], store_for: Callable[[str], object]) -> None:
    """Start the stitcher once per process (the dashboard's)."""
    global _thread
    with _guard:
        if _thread is not None and _thread.is_alive():
            return
        def loop():
            time.sleep(FIRST)
            while True:
                for p in profiles():
                    try:
                        last[p] = run_once(home_for(p), store_for(p))
                    except Exception:   # one profile's trouble never stops the others
                        log.exception("stitching %s", p)
                _wake.wait(EVERY)
                _wake.clear()
        _thread = threading.Thread(target=loop, name="domo-stitcher", daemon=True)
        _thread.start()
