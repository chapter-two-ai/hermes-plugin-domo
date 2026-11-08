"""Domo's store: decisions, to-dos, receipts, runs, briefs, facts and spaces for one Hermes profile.

One SQLite file per profile (``<profile home>/plugin-data/domo/domo.db``). Items share one table
with a JSON ``data`` column, so adding a field never needs a migration. Every write appends to
``changes`` so clients can poll ``since=<cursor>`` instead of re-reading everything. A space groups
chats, routines (Hermes cron jobs), files and items; items still name theirs in ``space``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

KINDS = ("decision", "todo", "receipt", "run", "brief", "fact", "suggestion")

# Statuses a client may filter on; "open" items are the ones still asking for attention.
OPEN_STATUSES = {
    "decision": ("open",),
    "todo": ("open",),
    "run": ("running", "stalled"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    space       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}',
    session_id  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_kind_status ON items(kind, status);
CREATE TABLE IF NOT EXISTS changes (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    space       TEXT NOT NULL DEFAULT '',
    asked       INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spaces (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    icon         TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    prompt       TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_spaces (
    session_id  TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
-- Memory, in three layers (after DeepTutor): what happened, what's known, and what it adds up to.
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     REAL NOT NULL,
    kind   TEXT NOT NULL,
    text   TEXT NOT NULL,
    ref    TEXT NOT NULL DEFAULT '',
    space  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS instincts (
    id          TEXT PRIMARY KEY,
    pattern     TEXT NOT NULL,
    norm        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'habit',
    confidence  REAL NOT NULL,
    evidence    INTEGER NOT NULL DEFAULT 1,
    origin      TEXT NOT NULL DEFAULT 'inferred',
    space       TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL DEFAULT '',
    hours       TEXT NOT NULL DEFAULT '',
    days        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS synthesis (
    section     TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestion_votes (
    key      TEXT NOT NULL,
    kind     TEXT NOT NULL,
    verdict  TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS suggestion_votes_key ON suggestion_votes(key);
CREATE TABLE IF NOT EXISTS archived_sessions (
    session_id   TEXT PRIMARY KEY,
    archived_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_spaces (
    job_id    TEXT PRIMARY KEY,
    space_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    space_id    TEXT,
    session_id  TEXT,
    created_at  REAL NOT NULL
);
"""

SPACE_FIELDS = ("id", "name", "icon", "color", "description", "prompt", "created_at", "updated_at", "parent_id", "origin")
PALETTE = ("#4CC9F0", "#FF6B2C", "#3DD68C", "#FF2D8A", "#FFB020", "#2F5BFF")   # the brand spectrum (docs/BRAND.md §3.2)

# Seeded spaces: icon, description, and the prompt the agent gets when a chat starts in the space.
SPACE_PRESETS = {
    "work": ("briefcase", "Your job: projects, colleagues, meetings and deadlines.",
             "You're helping with work. Keep answers crisp and decision-ready, and track commitments as to-dos. "
             "Ask before sending anything to colleagues or clients."),
    "coding": ("code", "Code, repos and long coding runs on this Mac.",
               "You're pairing on code. Work in the repo the person names, on a branch; track long jobs with domo_run; "
               "ask before pushing or merging.\n"
               "- Think first: say what you assume and what you'll change before you change it; ask when it's ambiguous.\n"
               "- Keep it simple: the smallest change that does the job, in the code's own style; no extras nobody asked for.\n"
               "- Touch only what the task needs; leave unrelated code alone, and say what you noticed instead.\n"
               "- Done means proven: run the tests or the program and show the result before you say it works.\n"
               "Method, when it fits: brainstorming then writing-plans for anything new, systematic-debugging for bugs, "
               "test-driven-development for logic, verification-before-completion before you report, "
               "to-questionnaire when they must choose, handoff when the chat gets long, archify to explain a system, "
               "impeccable for interface work."),
    "personal": ("leaf", "Health, errands, money and everything that's just yours.",
                 "You're helping with personal life. Be warm and brief, keep private things private, "
                 "and turn loose ends into to-dos."),
    "apartment hunt": ("house", "Listings, viewings, bids and the paperwork of finding a new home.",
                       "You're helping the two of them find a new apartment. Compare listings with what you know "
                       "they want and put viewings and bids up as decisions. Never contact agents or bid without asking."),
    "daughter": ("child", "School, health, activities and everything about our daughter.",
                 "You're helping with their daughter's life: school, health and activities. Keep both parents in "
                 "the loop and note dates as to-dos. Treat her details as private."),
    "trips": ("plane", "Travel plans, bookings and itineraries.",
              "You're planning trips. Gather options with prices and times and propose one plan. "
              "Ask before booking or paying for anything."),
    "home": ("house", "The household: bills, repairs, chores and shopping.",
             "You're running the household. Keep track of bills, repairs and chores, and split the work fairly. "
             "Ask before paying or ordering anything."),
    "family": ("people", "The extended family: birthdays, visits and plans together.",
               "You're helping the extended family coordinate. Keep replies short and friendly and sum plans up "
               "clearly for everyone. Ask before committing anyone to anything."),
}


SYNTHESIS_SECTIONS = ("who", "now", "preferences")


class StoreError(ValueError):
    """A request the store refuses (unknown item, wrong kind, bad status)."""


def store_path(profile_home: Path) -> Path:
    return Path(profile_home) / "plugin-data" / "domo" / "domo.db"


class Store:
    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        for sql in (
            # stores from before facts knew whether they were said or guessed
            "ALTER TABLE facts ADD COLUMN origin TEXT NOT NULL DEFAULT 'stated'",
            # sub-spaces (Work › Baltazar), and whether the person or Domo made a space
            "ALTER TABLE spaces ADD COLUMN parent_id TEXT",
            "ALTER TABLE spaces ADD COLUMN origin TEXT NOT NULL DEFAULT 'person'",
            # who filed a chat, how sure, and why; the person's filing is never moved by Domo
            "ALTER TABLE session_spaces ADD COLUMN filed_by TEXT NOT NULL DEFAULT 'person'",
            "ALTER TABLE session_spaces ADD COLUMN confidence REAL",
            "ALTER TABLE session_spaces ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
        ):
            try:
                self._db.execute(sql)
            except sqlite3.OperationalError:
                pass   # already there
        self._db.execute("CREATE TABLE IF NOT EXISTS filing_attempts (session_id TEXT PRIMARY KEY, at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0)")

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # ---- items -------------------------------------------------------------------------------

    def create(self, kind: str, title: str, *, body: str = "", space: str = "", status: str = "",
               data: Optional[dict] = None, session_id: str = "") -> dict:
        if kind not in KINDS or kind == "fact":
            raise StoreError(f"unknown item kind {kind!r}")
        title = (title or "").strip()
        if not title:
            raise StoreError("title is required")
        now = self._clock()
        item_id = f"{kind[:3]}_{uuid.uuid4().hex[:12]}"
        status = status or _default_status(kind)
        with self._tx() as db:
            db.execute(
                "INSERT INTO items (id, kind, status, space, title, body, data, session_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item_id, kind, status, space or "", title, body or "", json.dumps(data or {}),
                 session_id or "", now, now),
            )
            db.execute("INSERT INTO changes (item_id, at) VALUES (?, ?)", (item_id, now))
        return self.get(item_id)

    def get(self, item_id: str) -> dict:
        row = self._db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise StoreError(f"no item {item_id!r}")
        return _row(row)

    def update(self, item_id: str, *, status: Optional[str] = None, title: Optional[str] = None,
               body: Optional[str] = None, data: Optional[dict] = None) -> dict:
        """Patch an item. ``data`` is merged into the existing data, not replaced."""
        current = self.get(item_id)
        merged = {**current["data"], **(data or {})}
        now = self._clock()
        with self._tx() as db:
            db.execute(
                "UPDATE items SET status = ?, title = ?, body = ?, data = ?, updated_at = ? WHERE id = ?",
                (status or current["status"], title if title is not None else current["title"],
                 body if body is not None else current["body"], json.dumps(merged), now, item_id),
            )
            db.execute("INSERT INTO changes (item_id, at) VALUES (?, ?)", (item_id, now))
        return self.get(item_id)

    def list(self, kind: Optional[str] = None, *, status: Optional[str] = None, space: Optional[str] = None,
             since: float = 0.0, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM items WHERE updated_at >= ?", [since]
        if kind:
            sql += " AND kind = ?"; args.append(kind)
        if status == "open" and kind in OPEN_STATUSES:
            marks = ",".join("?" for _ in OPEN_STATUSES[kind])
            sql += f" AND status IN ({marks})"; args.extend(OPEN_STATUSES[kind])
        elif status:
            sql += " AND status = ?"; args.append(status)
        if space:
            sql += " AND space = ?"; args.append(space)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
        return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def cursor(self) -> int:
        row = self._db.execute("SELECT MAX(seq) FROM changes").fetchone()
        return int(row[0] or 0)

    def changed_since(self, seq: int) -> tuple[list[dict], int]:
        """Items changed after change ``seq``, newest state only, and the new cursor."""
        rows = self._db.execute(
            "SELECT DISTINCT item_id FROM changes WHERE seq > ? ORDER BY seq", (seq,)).fetchall()
        items = []
        for r in rows:
            try:
                items.append(self.get(r["item_id"]))
            except StoreError:
                continue
        return items, self.cursor()

    # ---- decisions ---------------------------------------------------------------------------

    def answer(self, decision_id: str, choice: str, *, note: str = "", by: str = "") -> dict:
        item = self.get(decision_id)
        if item["kind"] != "decision":
            raise StoreError(f"{decision_id} is a {item['kind']}, not a decision")
        if item["status"] != "open":
            raise StoreError(f"decision {decision_id} is already {item['status']}")
        options = item["data"].get("options") or []
        if options and choice not in options:
            raise StoreError(f"choice {choice!r} is not one of {options}")
        return self.update(decision_id, status="answered",
                           data={"choice": choice, "note": note, "answered_by": by,
                                 "answered_at": self._clock()})

    # ---- runs --------------------------------------------------------------------------------

    def run_beat(self, run_id: str, *, progress: Optional[float] = None, note: str = "",
                 cost_usd: Optional[float] = None) -> dict:
        item = self._require(run_id, "run")
        data: dict[str, Any] = {"last_beat_at": self._clock()}
        if progress is not None:
            data["progress"] = max(0.0, min(1.0, float(progress)))
        if note:
            data["note"] = note
        if cost_usd is not None:
            data["cost_usd"] = float(cost_usd)
        status = "running" if item["status"] == "stalled" else None
        return self.update(run_id, status=status, data=data)

    def run_finish(self, run_id: str, *, ok: bool, note: str = "", proof: str = "",
                   cost_usd: Optional[float] = None) -> dict:
        self._require(run_id, "run")
        data: dict[str, Any] = {"finished_at": self._clock(), "note": note, "proof": proof}
        if cost_usd is not None:
            data["cost_usd"] = float(cost_usd)
        if ok:
            data["progress"] = 1.0
        return self.update(run_id, status="done" if ok else "failed", data=data)

    def mark_stalled(self, now: Optional[float] = None) -> list[dict]:
        """Running runs whose heartbeat is older than their allowed silence become 'stalled'."""
        now = now if now is not None else self._clock()
        stalled = []
        for run in self.list("run", status="running", limit=500):
            limit_s = float(run["data"].get("stall_after_minutes") or 30) * 60
            last = float(run["data"].get("last_beat_at") or run["created_at"])
            if now - last > limit_s:
                stalled.append(self.update(run["id"], status="stalled"))
        return stalled

    # ---- briefs ------------------------------------------------------------------------------

    def latest_brief(self) -> Optional[dict]:
        briefs = self.list("brief", limit=1)
        return briefs[0] if briefs else None

    # ---- facts -------------------------------------------------------------------------------

    def set_fact(self, key: str, value: str, *, source: str = "", space: str = "", origin: str = "stated") -> dict:
        """``origin``: 'stated' when the person said it, 'inferred' when Domo worked it out (after Graphify)."""
        key = _fact_key(key)
        if not value:
            raise StoreError("value is required")
        if origin not in ("stated", "inferred"):
            raise StoreError("origin is stated or inferred")
        with self._tx() as db:
            db.execute(
                "INSERT INTO facts (key, value, source, space, origin, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source,"
                " space=excluded.space, origin=excluded.origin, updated_at=excluded.updated_at",
                (key, value, source, space, origin, self._clock()))
        return self.get_fact(key, count=False)

    def forget_fact(self, key: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM facts WHERE key = ?", (_fact_key(key),))

    # ---- memory: events, instincts, synthesis -----------------------------------------------

    def note(self, kind: str, text: str, *, ref: str = "", space: str = "") -> dict:
        """The trace: append-only, so a later consolidation can always go back to what happened."""
        text = " ".join((text or "").split())[:400]
        if not text:
            raise StoreError("text is required")
        with self._tx() as db:
            cur = db.execute("INSERT INTO events (at, kind, text, ref, space) VALUES (?,?,?,?,?)", (self._clock(), kind, text, ref, space))
        return dict(self._db.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone())

    def events(self, *, since: float = 0, limit: int = 100) -> list[dict]:
        rows = self._db.execute("SELECT * FROM events WHERE at > ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        return [dict(r) for r in rows]

    def learn(self, pattern: str, *, kind: str = "habit", origin: str = "inferred", space: str = "", prompt: str = "",
              hours: str = "", days: str = "") -> dict:
        """An instinct (after ECC): a pattern with a confidence that evidence raises and feedback lowers.
        Seen again, it gets stronger; told by the person, it starts strong."""
        pattern = " ".join((pattern or "").split())[:240]
        if not pattern:
            raise StoreError("pattern is required")
        if kind not in ("habit", "preference", "avoid"):
            raise StoreError("kind is habit, preference or avoid")
        norm = _fact_key(pattern)
        now = self._clock()
        row = self._db.execute("SELECT * FROM instincts WHERE norm = ?", (norm,)).fetchone()
        with self._tx() as db:
            if row:
                origin = "stated" if "stated" in (origin, row["origin"]) else "inferred"
                db.execute("UPDATE instincts SET confidence = MIN(0.95, confidence + 0.1), evidence = evidence + 1, origin = ?,"
                           " prompt = COALESCE(NULLIF(?, ''), prompt), hours = COALESCE(NULLIF(?, ''), hours),"
                           " days = COALESCE(NULLIF(?, ''), days), space = COALESCE(NULLIF(?, ''), space), updated_at = ? WHERE id = ?",
                           (origin, prompt, hours, days, space, now, row["id"]))
                iid = row["id"]
            else:
                iid = f"in_{uuid.uuid4().hex[:12]}"
                db.execute("INSERT INTO instincts (id, pattern, norm, kind, confidence, origin, space, prompt, hours, days, created_at, updated_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (iid, pattern, norm, kind, 0.8 if origin == "stated" else 0.35, origin, space, prompt, hours, days, now, now))
        return self.instinct(iid)

    def instinct(self, instinct_id: str) -> dict:
        row = self._db.execute("SELECT * FROM instincts WHERE id = ?", (instinct_id,)).fetchone()
        if row is None:
            raise StoreError(f"no instinct {instinct_id!r}")
        return dict(row)

    def weaken(self, instinct_id: str, by: float = 0.2) -> dict:
        """Counter-evidence or a 'less like this'; below 0.1 it's gone."""
        current = self.instinct(instinct_id)
        with self._tx() as db:
            if current["confidence"] - by < 0.1:
                db.execute("DELETE FROM instincts WHERE id = ?", (instinct_id,))
                return {**current, "confidence": 0.0, "forgotten": True}
            db.execute("UPDATE instincts SET confidence = confidence - ?, updated_at = ? WHERE id = ?", (by, self._clock(), instinct_id))
        return self.instinct(instinct_id)

    def forget_instinct(self, instinct_id: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM instincts WHERE id = ?", (instinct_id,))

    def instincts(self, *, at_least: float = 0.0) -> list[dict]:
        rows = self._db.execute("SELECT * FROM instincts WHERE confidence >= ? ORDER BY confidence DESC, updated_at DESC", (at_least,))
        return [dict(r) for r in rows]

    def set_synthesis(self, section: str, text: str) -> None:
        """The top layer: a short, rewritten picture of the person (who, now, preferences)."""
        if section not in SYNTHESIS_SECTIONS:
            raise StoreError(f"section is one of {', '.join(SYNTHESIS_SECTIONS)}")
        with self._tx() as db:
            if text.strip():
                db.execute("INSERT INTO synthesis (section, text, updated_at) VALUES (?,?,?) ON CONFLICT(section)"
                           " DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at", (section, text.strip()[:1200], self._clock()))
            else:
                db.execute("DELETE FROM synthesis WHERE section = ?", (section,))

    def synthesis(self) -> dict[str, dict]:
        return {r["section"]: dict(r) for r in self._db.execute("SELECT * FROM synthesis")}

    def get_fact(self, key: str, *, count: bool = True) -> dict:
        """Look a fact up. ``count`` records that someone had to ask again (a memory miss)."""
        key = _fact_key(key)
        if count:
            with self._tx() as db:
                db.execute("UPDATE facts SET asked = asked + 1 WHERE key = ?", (key,))
        row = self._db.execute("SELECT * FROM facts WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise StoreError(f"no fact {key!r}")
        return dict(row)

    def facts(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM facts ORDER BY key").fetchall()]

    # ---- spaces ------------------------------------------------------------------------------
    # A space's changes are logged as ``space:<id>`` so /changes pollers notice them too.

    def seed_spaces(self, names: list[str]) -> list[dict]:
        """Defaults for a new profile; never again once spaces have existed (deleting all stays deleted)."""
        if self._db.execute("SELECT 1 FROM spaces UNION ALL SELECT 1 FROM changes WHERE item_id LIKE 'space:%'"
                            " LIMIT 1").fetchone():
            return []
        seeded = []
        for i, name in enumerate(n.strip() for n in names if n.strip()):
            icon, description, prompt = SPACE_PRESETS.get(name.lower(), ("", "", ""))
            seeded.append(self.create_space(name, icon=icon, color=PALETTE[i % len(PALETTE)],
                                            description=description, prompt=prompt))
        return seeded

    def spaces(self) -> list[dict]:
        """Every space with its counts, parents before their sub-spaces, adopting space names items use
        that have no record yet."""
        known = {k for r in self._db.execute("SELECT id, name FROM spaces") for k in (r["id"], r["name"].lower())}
        for (name,) in self._db.execute("SELECT DISTINCT space FROM items WHERE space != ''").fetchall():
            if name.lower() not in known:
                known.update((self.create_space(name)["id"], name.lower()))
        rows = self._db.execute("SELECT * FROM spaces ORDER BY position, name").fetchall()
        tops = [r for r in rows if not r["parent_id"]]
        kids = {}
        for r in rows:
            if r["parent_id"]:
                kids.setdefault(r["parent_id"], []).append(r)
        ordered = [x for t in tops for x in (t, *sorted(kids.get(t["id"], []), key=lambda k: k["name"].lower()))]
        return [{**_space(r), "counts": self._space_counts(r)} for r in ordered]

    def children(self, space_id: str) -> list[dict]:
        rows = self._db.execute("SELECT * FROM spaces WHERE parent_id = ? ORDER BY name", (space_id,)).fetchall()
        return [{**_space(r), "counts": self._space_counts(r)} for r in rows]

    def lineage(self, space_id: str) -> list[dict]:
        """The space and its parent, outermost first: [Work, Baltazar]."""
        s = self.space(space_id)
        if not s:
            return []
        parent = self.space(s["parent_id"]) if s.get("parent_id") else None
        return [parent, s] if parent else [s]

    def space(self, space_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM spaces WHERE id = ?", (space_id or "",)).fetchone()
        return _space(row) if row else None

    def _space_counts(self, row: sqlite3.Row) -> dict:
        one = lambda sql, *a: self._db.execute(sql, a).fetchone()[0]  # noqa: E731
        return {"chats": one("SELECT COUNT(*) FROM session_spaces WHERE space_id = ?", row["id"]),
                "files": one("SELECT COUNT(*) FROM files WHERE space_id = ?", row["id"]),
                "routines": one("SELECT COUNT(*) FROM job_spaces WHERE space_id = ?", row["id"]),
                "open": len(self.space_items(row["id"]))}

    def space_items(self, space_id: str) -> list[dict]:
        """Open to-dos and decisions filed under the space, by name or id."""
        space = self.space(space_id)
        if not space:
            return []
        rows = self._db.execute(
            "SELECT * FROM items WHERE kind IN ('todo', 'decision') AND status = 'open' AND space IN (?, ?)"
            " ORDER BY created_at DESC, rowid DESC", (space["name"], space["id"])).fetchall()
        return [_row(r) for r in rows]

    def create_space(self, name: str, *, icon: str = "", color: str = "", description: str = "",
                     prompt: str = "", parent_id: Optional[str] = None, origin: str = "person") -> dict:
        name = " ".join((name or "").split())
        if not name:
            raise StoreError("name is required")
        parent = None
        if parent_id:
            parent = self.space(parent_id)
            if not parent:
                raise StoreError(f"no space {parent_id!r}")
            if parent.get("parent_id"):
                raise StoreError("sub-spaces go one level deep")
        if self._space_named(name, parent_id):
            raise StoreError(f"space {name!r} already exists" + (f" in {parent['name']}" if parent else ""))
        if parent:   # a sub-space wears its parent's colour unless it's given one
            color = color or parent["color"]
            icon = icon or parent["icon"]
        now = self._clock()
        with self._tx() as db:
            base = f"{parent_id}-{_slug(name)}" if parent_id else _slug(name)
            space_id, n = base, 1
            while db.execute("SELECT 1 FROM spaces WHERE id = ?", (space_id,)).fetchone():
                n += 1
                space_id = f"{base}-{n}"
            position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM spaces").fetchone()[0]
            db.execute("INSERT INTO spaces (id, name, icon, color, description, prompt, created_at, updated_at, position, parent_id, origin)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (space_id, name, icon or "", color or "", description or "", prompt or "", now, now, position,
                        parent_id or None, origin if origin in ("person", "domo") else "person"))
            _touch(db, space_id, now)
        return self.space(space_id)

    def update_space(self, space_id: str, **fields: Optional[str]) -> dict:
        """Patch a space. A rename carries the items and facts filed under the old name along."""
        current = self.space(space_id)
        if not current:
            raise StoreError(f"no space {space_id!r}")
        patch = {k: v for k, v in fields.items() if v is not None and k in SPACE_FIELDS[1:6]}
        if "name" in patch:
            patch["name"] = " ".join(patch["name"].split())
            if not patch["name"]:
                raise StoreError("name is required")
            other = self._space_named(patch["name"], current.get("parent_id"))
            if other and other["id"] != space_id:
                raise StoreError(f"space {patch['name']!r} already exists")
        now = self._clock()
        if patch and current.get("origin") == "domo":
            patch["origin"] = "person"   # touched by the person: theirs now
        with self._tx() as db:
            if patch:
                sets = ", ".join(f"{k} = ?" for k in patch)
                db.execute(f"UPDATE spaces SET {sets}, updated_at = ? WHERE id = ?", (*patch.values(), now, space_id))
            if patch.get("name", current["name"]) != current["name"]:
                _refile(db, current["name"], patch["name"], now)
            _touch(db, space_id, now)
        return self.space(space_id)

    def delete_space(self, space_id: str) -> None:
        """Remove a space and its tags. Files stay, unfiled; items filed under it lose the space.
        A sub-space's chats move up to its parent; a parent takes its sub-spaces with it."""
        current = self.space(space_id)
        if not current:
            raise StoreError(f"no space {space_id!r}")
        for child in self.children(space_id):
            self.delete_space(child["id"])
        now = self._clock()
        with self._tx() as db:
            if current.get("parent_id"):
                db.execute("UPDATE session_spaces SET space_id = ?, updated_at = ? WHERE space_id = ?", (current["parent_id"], now, space_id))
            db.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
            db.execute("DELETE FROM session_spaces WHERE space_id = ?", (space_id,))
            db.execute("DELETE FROM job_spaces WHERE space_id = ?", (space_id,))
            db.execute("UPDATE files SET space_id = NULL WHERE space_id = ?", (space_id,))
            _refile(db, current["name"], "", now)
            _refile(db, space_id, "", now)
            _touch(db, space_id, now)

    def _space_named(self, name: str, parent_id: Optional[str] = None) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM spaces WHERE lower(name) = lower(?) AND COALESCE(parent_id, '') = ?",
                               (name, parent_id or "")).fetchone()
        return _space(row) if row else None

    def spaces_changed_since(self, seq: int) -> list[str]:
        rows = self._db.execute("SELECT DISTINCT item_id FROM changes WHERE seq > ? AND item_id LIKE 'space:%'"
                                " ORDER BY item_id", (seq,)).fetchall()
        return [r[0].removeprefix("space:") for r in rows]

    # ---- what belongs to a space -------------------------------------------------------------

    def tag_session(self, session_id: str, space_id: Optional[str], *, by: str = "person",
                    confidence: Optional[float] = None, reason: str = "") -> Optional[str]:
        """File a chat under a space (None unfiles it). Domo files only what the person hasn't: their
        filing, or their unfiling, stands."""
        old = self.session_space(session_id)
        if by == "domo" and old and old["filed_by"] == "person":
            return old["space_id"] or None
        now = self._clock()
        with self._tx() as db:
            if not space_id and by == "person" and old:
                # unfiled by hand: remembered as the person's choice, so Domo leaves it unfiled
                db.execute("UPDATE session_spaces SET space_id = '', filed_by = 'person', confidence = NULL, reason = '',"
                           " updated_at = ? WHERE session_id = ?", (now, session_id))
            elif not space_id:
                db.execute("DELETE FROM session_spaces WHERE session_id = ?", (session_id,))
            else:
                db.execute("INSERT INTO session_spaces (session_id, space_id, updated_at, filed_by, confidence, reason) VALUES (?,?,?,?,?,?)"
                           " ON CONFLICT(session_id) DO UPDATE SET space_id=excluded.space_id, updated_at=excluded.updated_at,"
                           " filed_by=excluded.filed_by, confidence=excluded.confidence, reason=excluded.reason",
                           (session_id, space_id, now, by, confidence, reason[:200]))
            for touched in {old and old["space_id"], space_id} - {None, ""}:
                _touch(db, touched, now)
        return space_id or None

    def filing_attempted(self, session_id: str, revision: int) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO filing_attempts (session_id, at, revision) VALUES (?, ?, ?)", (session_id, self._clock(), revision))

    def filing_attempts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._db.execute("SELECT session_id, revision FROM filing_attempts")}

    def filings(self) -> dict[str, dict]:
        """Every chat's filing: space, who, how sure, why (an empty space: unfiled by the person)."""
        return {r["session_id"]: dict(r) for r in self._db.execute("SELECT * FROM session_spaces")}

    def session_space(self, session_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM session_spaces WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    # ---- feedback on suggestions -----------------------------------------------------------

    def vote(self, key: str, kind: str, verdict: str) -> None:
        """done, dismiss, more or less: hides a suggestion, or tunes how much its kind counts."""
        if verdict not in ("done", "dismiss", "more", "less"):
            raise StoreError("verdict is done, dismiss, more or less")
        with self._tx() as db:
            db.execute("INSERT INTO suggestion_votes (key, kind, verdict, at) VALUES (?, ?, ?, ?)", (key, kind, verdict, self._clock()))

    def hidden_suggestions(self, *, dismissed_for: float = 7 * 86400) -> set[str]:
        """Done is done; a dismissal lasts a week, then the thing may come back if it's still true."""
        now = self._clock()
        rows = self._db.execute("SELECT key, verdict, MAX(at) FROM suggestion_votes WHERE verdict IN ('done', 'dismiss') GROUP BY key, verdict")
        return {k for k, v, at in rows if v == "done" or now - at < dismissed_for}

    def kind_weights(self) -> dict[str, float]:
        """More like this lifts a kind, less like this lowers it; dismissals nudge it down."""
        w: dict[str, float] = {}
        for kind, verdict in self._db.execute("SELECT kind, verdict FROM suggestion_votes WHERE at > ?", (self._clock() - 60 * 86400,)):
            w[kind] = w.get(kind, 1.0) * {"more": 1.15, "less": 0.8, "dismiss": 0.97, "done": 1.03}[verdict]
        return {k: max(0.3, min(1.6, v)) for k, v in w.items()}

    def set_archived(self, session_id: str, archived: bool) -> None:
        """Remember what was archived here: Hermes's own listing leaves archived chats out entirely."""
        with self._tx() as db:
            if archived:
                db.execute("INSERT OR REPLACE INTO archived_sessions (session_id, archived_at) VALUES (?, ?)",
                           (session_id, self._clock()))
            else:
                db.execute("DELETE FROM archived_sessions WHERE session_id = ?", (session_id,))

    def archived(self) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT session_id FROM archived_sessions ORDER BY archived_at DESC")]

    def session_tags(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self._db.execute("SELECT session_id, space_id FROM session_spaces WHERE space_id != ''")}

    def tag_job(self, job_id: str, space_id: Optional[str]) -> Optional[str]:
        old = self.job_tags().get(job_id)
        with self._tx() as db:
            if space_id:
                db.execute("INSERT INTO job_spaces (job_id, space_id) VALUES (?, ?)"
                           " ON CONFLICT(job_id) DO UPDATE SET space_id=excluded.space_id", (job_id, space_id))
            else:
                db.execute("DELETE FROM job_spaces WHERE job_id = ?", (job_id,))
            for touched in {old, space_id} - {None, ""}:
                _touch(db, touched, self._clock())
        return space_id or None

    def job_tags(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self._db.execute("SELECT job_id, space_id FROM job_spaces")}

    def add_file(self, path: str, name: str, mime: str, size: int, *, space_id: Optional[str] = None,
                 session_id: Optional[str] = None) -> dict:
        now = self._clock()
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO files (path, name, mime, size, space_id, session_id, created_at)"
                       " VALUES (?,?,?,?,?,?,?)", (path, name, mime, size, space_id or None, session_id or None, now))
            if space_id:
                _touch(db, space_id, now)
        return self.file(path)

    def file(self, path: str) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def link_file(self, path: str, *, session_id: str, space_id: Optional[str]) -> None:
        """An attachment sent in a chat belongs to that chat (and its space) unless it was filed already."""
        with self._tx() as db:
            db.execute("UPDATE files SET session_id = COALESCE(session_id, ?), space_id = COALESCE(space_id, ?)"
                       " WHERE path = ?", (session_id or None, space_id or None, path))

    def files(self, space_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM files", []
        if space_id:
            sql += " WHERE space_id = ?"; args.append(space_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- summary -----------------------------------------------------------------------------

    def feed(self, *, now: Optional[float] = None) -> dict:
        """Everything the home screen needs in one read."""
        now = now if now is not None else self._clock()
        self.mark_stalled(now)
        day_start = now - 24 * 3600
        runs = self.list("run", limit=50)
        active = [r for r in runs if r["status"] in ("running", "stalled")]
        recent = [r for r in runs if r["status"] in ("done", "failed") and r["updated_at"] >= day_start]
        decisions = self.list("decision", status="open")
        todos = self.list("todo", status="open")
        receipts = [r for r in self.list("receipt", limit=50) if r["created_at"] >= day_start]
        return {
            "brief": self.latest_brief(),
            "decisions": decisions,
            "todos": todos,
            "runs": active + recent,
            "receipts": receipts,
            "counts": {
                "needs_you": len(decisions) + sum(1 for r in active if r["status"] == "stalled"),
                "running": sum(1 for r in active if r["status"] == "running"),
                "done_today": len(recent),
                "spent_today_usd": round(sum(float(r["data"].get("cost_usd") or 0) for r in recent + active), 2),
            },
            "cursor": self.cursor(),
        }

    def _require(self, item_id: str, kind: str) -> dict:
        item = self.get(item_id)
        if item["kind"] != kind:
            raise StoreError(f"{item_id} is a {item['kind']}, not a {kind}")
        return item


def _default_status(kind: str) -> str:
    return {"decision": "open", "todo": "open", "run": "running", "receipt": "done", "brief": "published", "suggestion": "open"}[kind]


def _fact_key(key: str) -> str:
    key = " ".join((key or "").lower().split())
    if not key:
        raise StoreError("key is required")
    return key


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["data"] = json.loads(item["data"] or "{}")
    return item


def _space(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in SPACE_FIELDS}


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:48] or "space"


def _touch(db: sqlite3.Connection, space_id: str, now: float) -> None:
    db.execute("INSERT INTO changes (item_id, at) VALUES (?, ?)", (f"space:{space_id}", now))


def _refile(db: sqlite3.Connection, old: str, new: str, now: float) -> None:
    """Move items and facts filed under the space name ``old`` to ``new``, logging the items."""
    ids = [r[0] for r in db.execute("SELECT id FROM items WHERE space = ?", (old,)).fetchall()]
    db.execute("UPDATE items SET space = ?, updated_at = ? WHERE space = ?", (new, now, old))
    db.execute("UPDATE facts SET space = ? WHERE space = ?", (new, old))
    db.executemany("INSERT INTO changes (item_id, at) VALUES (?, ?)", [(i, now) for i in ids])
