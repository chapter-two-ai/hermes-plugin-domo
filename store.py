"""Domo's store: decisions, to-dos, receipts, runs, briefs and facts for one Hermes profile.

One SQLite file per profile (``<profile home>/plugin-data/domo/domo.db``). Items share one table
with a JSON ``data`` column, so adding a field never needs a migration. Every write appends to
``changes`` so clients can poll ``since=<cursor>`` instead of re-reading everything.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

KINDS = ("decision", "todo", "receipt", "run", "brief", "fact")

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
"""


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

    def set_fact(self, key: str, value: str, *, source: str = "", space: str = "") -> dict:
        key = _fact_key(key)
        if not value:
            raise StoreError("value is required")
        with self._tx() as db:
            db.execute(
                "INSERT INTO facts (key, value, source, space, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source,"
                " space=excluded.space, updated_at=excluded.updated_at",
                (key, value, source, space, self._clock()))
        return self.get_fact(key, count=False)

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
    return {"decision": "open", "todo": "open", "run": "running", "receipt": "done", "brief": "published"}[kind]


def _fact_key(key: str) -> str:
    key = " ".join((key or "").lower().split())
    if not key:
        raise StoreError("key is required")
    return key


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["data"] = json.loads(item["data"] or "{}")
    return item
