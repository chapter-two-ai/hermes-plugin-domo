"""The tools Domo gives the agent. Each handler returns a JSON string and never raises."""

from __future__ import annotations

import time

import contextvars
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .store import Store, StoreError, store_path

log = logging.getLogger(__name__)

_stores: dict[str, Store] = {}


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


home_resolver: Callable[[], Path] = _home
# The profile home each tool call writes to. A multiplexed gateway loads plugins once per profile
# and runs tool handlers on worker threads, so the home is bound at registration (see
# bound_tools) rather than looked up per call; this is the fallback for direct use and tests.
_bound_home: "contextvars.ContextVar[Optional[Path]]" = contextvars.ContextVar("domo_home", default=None)


def store_for(home: Path) -> Store:
    path = str(store_path(home))
    store = _stores.get(path)
    if store is None:
        store = _stores[path] = Store(Path(path))
    return store


def current_store() -> Store:
    return store_for(_bound_home.get() or home_resolver())


def bound_tools(home: Path) -> list[tuple[str, dict, Callable[..., str], str]]:
    """TOOLS with every handler pinned to one profile's store."""
    def pin(handler: Callable[..., str]) -> Callable[..., str]:
        def pinned(args: dict, **kwargs: Any) -> str:
            token = _bound_home.set(home)
            try:
                return handler(args, **kwargs)
            finally:
                _bound_home.reset(token)
        pinned.__name__ = handler.__name__
        return pinned
    return [(name, schema, pin(handler), emoji) for name, schema, handler, emoji in TOOLS]


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload})


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


def _guard(fn: Callable[..., str]) -> Callable[..., str]:
    def handler(args: dict, **kwargs: Any) -> str:
        try:
            return fn(args or {}, kwargs.get("session_id") or "")
        except StoreError as exc:
            return _fail(str(exc))
        except Exception as exc:  # a tool must never take the turn down
            log.exception("domo tool %s failed", fn.__name__)
            return _fail(f"internal error: {exc}")
    handler.__name__ = fn.__name__
    return handler


def _brief_item(item: dict) -> dict:
    return {k: item[k] for k in ("id", "kind", "status", "title", "space")}


# ---- handlers ---------------------------------------------------------------------------------

@_guard
def domo_ask(args: dict, session_id: str) -> str:
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
    if len(options) < 2:
        return _fail("give at least two options, the safe one first")
    item = current_store().create(
        "decision", args.get("title", ""), body=args.get("body", ""), space=args.get("space", ""),
        session_id=session_id,
        data={"options": options, "risk": args.get("risk", "reversible"),
              "kind": args.get("kind", "approve"), "details": args.get("details", "")})
    return _ok(decision=_brief_item(item),
               next="Asked. Stop here and tell the person briefly what you asked; the answer comes back"
                    " as a new message later.")


@_guard
def domo_todo(args: dict, session_id: str) -> str:
    store = current_store()
    action = args.get("action", "add")
    if action == "done":
        item = store.update(args.get("id", ""), status="done")
        return _ok(todo=_brief_item(item))
    owner = args.get("owner", "me")
    if owner not in ("me", "domo"):
        return _fail("owner is 'me' or 'domo'")
    item = store.create("todo", args.get("title", ""), body=args.get("body", ""), space=args.get("space", ""),
                        session_id=session_id, data={"owner": owner, "due": args.get("due", "")})
    return _ok(todo=_brief_item(item))


@_guard
def domo_receipt(args: dict, session_id: str) -> str:
    data = {"proof": args.get("proof", ""), "undo": args.get("undo", "")}
    if args.get("cost_usd") is not None:
        data["cost_usd"] = float(args["cost_usd"])
    item = current_store().create("receipt", args.get("title", ""), body=args.get("detail", ""),
                                  space=args.get("space", ""), session_id=session_id, data=data)
    return _ok(receipt=_brief_item(item))


@_guard
def domo_run(args: dict, session_id: str) -> str:
    store = current_store()
    action = args.get("action", "")
    if action == "start":
        data = {"done_means": args.get("done_means", ""), "progress": 0.0, "repo": args.get("repo", ""),
                "expected_minutes": args.get("expected_minutes"), "budget_usd": args.get("budget_usd"),
                "stall_after_minutes": args.get("stall_after_minutes") or 30,
                "needs": args.get("needs", "")}
        item = store.create("run", args.get("title", ""), body=args.get("note", ""),
                            space=args.get("space", ""), session_id=session_id, data=data)
        return _ok(run=_brief_item(item), next="Send action='beat' while it works and 'finish' or 'fail' at the end.")
    run_id = args.get("id", "")
    if action == "beat":
        item = store.run_beat(run_id, progress=args.get("progress"), note=args.get("note", ""),
                              cost_usd=args.get("cost_usd"))
    elif action in ("finish", "fail"):
        item = store.run_finish(run_id, ok=action == "finish", note=args.get("note", ""),
                                proof=args.get("proof", ""), cost_usd=args.get("cost_usd"))
    else:
        return _fail("action is start, beat, finish or fail")
    return _ok(run=_brief_item(item))


@_guard
def domo_brief(args: dict, session_id: str) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return _fail("text is required")
    title = (args.get("title") or text.split(".")[0])[:120]
    item = current_store().create("brief", title, body=text, session_id=session_id,
                                  data={"highlights": args.get("highlights") or []})
    return _ok(brief=_brief_item(item))


@_guard
def domo_fact(args: dict, session_id: str) -> str:
    store = current_store()
    if args.get("action") == "set":
        fact = store.set_fact(args.get("key", ""), args.get("value", ""), source=args.get("source", ""),
                              space=args.get("space", ""))
    else:
        fact = store.get_fact(args.get("key", ""))
    return _ok(fact=fact)


@_guard
def domo_suggest(args: dict, session_id: str) -> str:
    title = (args.get("title") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not title or not prompt:
        return _fail("title and prompt are required")
    hours = float(args.get("expires_hours") or 36)
    item = current_store().create("suggestion", title[:140], body=(args.get("because") or "").strip()[:240], space=args.get("space", ""),
                                  session_id=session_id, data={"prompt": prompt, "expires_at": time.time() + hours * 3600})
    return _ok(suggestion=_brief_item(item))


@_guard
def domo_open(args: dict, session_id: str) -> str:
    store = current_store()
    kinds = [args["kind"]] if args.get("kind") else ["decision", "todo", "run"]
    items = [_brief_item(i) for k in kinds for i in store.list(k, status="open", limit=50)]
    return _ok(items=items)


# ---- schemas ----------------------------------------------------------------------------------

def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_S = {"type": "string"}
_SPACE = {"type": "string", "description": "Space this belongs to, e.g. Work, Coding, Personal, Apartment hunt"}

TOOLS: list[tuple[str, dict, Callable[..., str], str]] = [
    ("domo_ask", _schema(
        "domo_ask",
        "Ask the person for a decision instead of acting: anything outbound (sending, booking), paying, deleting,"
        " or a choice only they can make. It shows as a card in the Domo app and on their phone. Then stop the turn.",
        {"title": {"type": "string", "description": "Plain-language question, e.g. 'Book Ekstedt Sat 19:30 for 4?'"},
         "body": {"type": "string", "description": "One or two sentences with the facts that matter"},
         "options": {"type": "array", "items": _S, "description": "Two to four answers, the safe one first"},
         "risk": {"type": "string", "enum": ["reversible", "outbound", "money"]},
         "kind": {"type": "string", "enum": ["approve", "pick", "send", "credential", "delegate", "routine"]},
         "details": {"type": "string", "description": "The exact action or payload, shown behind a disclosure"},
         "space": _SPACE},
        ["title", "options"]), domo_ask, "🙋"),
    ("domo_todo", _schema(
        "domo_todo", "Add a to-do (owner 'me' for the person, 'domo' for work you will do once approved) or mark one done.",
        {"action": {"type": "string", "enum": ["add", "done"]}, "title": _S, "body": _S,
         "owner": {"type": "string", "enum": ["me", "domo"]}, "due": _S,
         "id": {"type": "string", "description": "For action 'done'"}, "space": _SPACE},
        []), domo_todo, "☑️"),
    ("domo_receipt", _schema(
        "domo_receipt", "Record something you did, with proof (a confirmation number, link, file) and how to undo it.",
        {"title": _S, "detail": _S, "proof": _S, "undo": _S, "cost_usd": {"type": "number"}, "space": _SPACE},
        ["title"]), domo_receipt, "🧾"),
    ("domo_run", _schema(
        "domo_run", "Track a long job so the person is told when it stalls or finishes instead of asking.",
        {"action": {"type": "string", "enum": ["start", "beat", "finish", "fail"]},
         "id": {"type": "string", "description": "Run id, for beat/finish/fail"},
         "title": _S, "done_means": {"type": "string", "description": "What finished looks like"},
         "expected_minutes": {"type": "number"}, "budget_usd": {"type": "number"},
         "stall_after_minutes": {"type": "number"}, "needs": {"type": "string", "description": "Credentials it depends on"},
         "progress": {"type": "number", "description": "0 to 1"}, "note": _S, "proof": _S,
         "cost_usd": {"type": "number"}, "space": _SPACE,
         "repo": {"type": "string", "description": "For coding runs: the repo's path on this Mac"}},
        ["action"]), domo_run, "🏃"),
    ("domo_brief", _schema(
        "domo_brief", "Publish the brief shown on the Domo home screen: prose, under 200 words, decisions first.",
        {"title": _S, "text": _S, "highlights": {"type": "array", "items": _S}},
        ["text"]), domo_brief, "📰"),
    ("domo_fact", _schema(
        "domo_fact", "Save a stable fact (VAT number, address, contact) or look one up, so it is answered once.",
        {"action": {"type": "string", "enum": ["set", "get"]}, "key": _S, "value": _S, "source": _S, "space": _SPACE},
        ["action", "key"]), domo_fact, "📌"),
    ("domo_suggest", _schema(
        "domo_suggest",
        "Leave the person a suggestion for the next time they open Domo: something prepared and ready to go"
        " (a drafted reply, a next step on a repo, a plan for tomorrow). It appears on their home screen with your"
        " reason; tapping it opens a chat with the prompt filled in. Read and prepare only; never act for them here.",
        {"title": {"type": "string", "description": "What to do, as a short imperative: 'Reply to Anna about Thursday'"},
         "because": {"type": "string", "description": "Why now, in one line, naming the source: 'She asked yesterday; you're free 14:00'"},
         "prompt": {"type": "string", "description": "The message that starts the chat when they tap it"},
         "expires_hours": {"type": "number", "description": "When it stops being worth showing (default 36)"},
         "space": _SPACE},
        ["title", "prompt"]), domo_suggest, "💡"),
    ("domo_open", _schema(
        "domo_open", "List open decisions, to-dos and runs, to avoid asking twice.",
        {"kind": {"type": "string", "enum": ["decision", "todo", "run"]}}, []), domo_open, "📋"),
]

PROMPT = (
    "You are Domo, this person's chief of staff. Prepare and do the work; bring them decisions, not questions.\n"
    "- Before anything outbound, paying, booking or deleting, call domo_ask and stop the turn.\n"
    "- Long jobs: domo_run start, beat while working, finish with proof.\n"
    "- After you act, domo_receipt with proof and how to undo.\n"
    "- Facts people ask for again: domo_fact.\n"
    "- Check domo_open before asking something you may already have asked.\n"
    "- When you see something they will want to do next (a reply owed, a next step, a fix), prepare it and"
    " leave it with domo_suggest instead of saying 'let me know'."
)
