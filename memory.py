"""What Domo knows about the person, in three layers (after DeepTutor's file memory):

1. the trace: ``events``, append-only, of what happened (a suggestion taken, a decision answered,
   a chat started in a space);
2. what's known: facts (stated by the person, or inferred) and instincts (after ECC): patterns with a
   confidence that evidence raises and feedback lowers;
3. the synthesis: a short rewritten picture (who they are, what's going on now, what they prefer).

The nightly "Domo ideas" routine consolidates 1 into 2 and 3 (skills/domo-ideas). Next up and every
chat turn read the top: only instincts above ``STRONG`` reach either, so a hunch never steers.

Hermes's own memory stays as it is; this is Domo's, local, and editable on the Memory screen.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

STRONG = 0.6
PROMPT_LIMIT = 900


def recall(store, *, since: float = 0, events: int = 80) -> dict:
    return {
        "synthesis": {k: v["text"] for k, v in store.synthesis().items()},
        "instincts": store.instincts(),
        "facts": store.facts(),
        "events": store.events(since=since, limit=events),
    }


def for_prompt(store) -> str:
    """What every chat turn is told, kept short: the synthesis and the strong instincts."""
    parts = []
    syn = store.synthesis()
    for section, label in (("who", "Who"), ("now", "Now"), ("preferences", "Prefers")):
        if section in syn:
            parts.append(f"{label}: {syn[section]['text']}")
    strong = [i for i in store.instincts(at_least=STRONG) if i["kind"] in ("preference", "avoid")][:6]
    if strong:
        parts.append("Patterns: " + "; ".join(("avoid: " if i["kind"] == "avoid" else "") + i["pattern"] for i in strong))
    if not parts:
        return ""
    text = "[What Domo knows about you, from its memory; correct it if it's wrong]\n" + "\n".join(parts)
    return text if len(text) <= PROMPT_LIMIT else text[: PROMPT_LIMIT - 1].rsplit(" ", 1)[0] + "…"


def _in_range(spec: str, value: int) -> bool:
    """'8-10', '1-5', '6,0' or '' (always)."""
    if not spec.strip():
        return True
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            if lo.isdigit() and hi.isdigit() and int(lo) <= value <= int(hi):
                return True
        elif part.isdigit() and int(part) == value:
            return True
    return False


def habits_due(store, *, now: Optional[float] = None) -> list[dict]:
    """Strong habits whose time is now become Next up suggestions, with the instinct as their reason."""
    t = datetime.fromtimestamp(now if now is not None else time.time())
    weekday = (t.weekday() + 1) % 7   # cron's: 0 is Sunday
    out = []
    for i in store.instincts(at_least=STRONG):
        if i["kind"] != "habit" or not i["prompt"] or not (i["hours"] or i["days"]):
            continue
        if _in_range(i["hours"], t.hour) and _in_range(i["days"], weekday):
            out.append({"kind": "habit", "key": f"habit:{i['id']}:{t:%Y-%m-%d}", "title": i["pattern"][:90], "at": i["updated_at"],
                        "space": i["space"], "instinct": i["id"],
                        "because": f"You usually do this now · seen {i['evidence']}× · {round(i['confidence'] * 100)}% sure",
                        "action": {"type": "chat", "prefill": i["prompt"]}})
    return out
