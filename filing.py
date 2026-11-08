"""Tagging: every chat in its places without being asked. One area (Work, Coding, Personal, Lykke) and up
to three topics (Baltazar, Domo, Thailand), reusing what exists and making a topic only when the chat
keeps naming it.

The person just starts chatting. After a chat's first turns, and again from the stitcher for anything
new, Domo asks Jev (TypeSafe's decision model: typed answers with calibrated confidence, in a few
hundred milliseconds, for a fraction of a cent) in one request:

- which area is this about (a choice among the person's areas, or none)?
- for each topic it might be about (the ones it names, then the most recent): is it? (a yes/no each)
- for each name the chat keeps coming back to that isn't a topic yet: is it mainly about that?

Jev chooses; it doesn't write. So a new topic's name comes from the chat itself (``candidates``: proper
nouns, quoted names, repo names). Code, not the model, owns the thresholds. A tag the person put on or
took off is never touched (store.tag).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Optional

from . import search

log = logging.getLogger("domo.filing")

SPACE_MIN = 0.6        # sure enough of the area to tag it
NEW_MIN = 0.85         # sure enough of a new topic to make it
MAX_TEXT = 4000
SKIP_TITLES = {"domo inbox"}

# words that are capitalised without being a name, in English and Swedish
NOT_NAMES = set("""
I I'm I'll I've I'd A An The This That These Those It Its We You He She They My Our Your His Her Their Me Us Them
Can Could Would Should Will Shall May Might Must Do Does Did Is Are Was Were Be Been Have Has Had Please Thanks Thank
Hi Hey Hello Ok Okay Yes No Not And Or But If Then So Also Just What When Where Why How Which Who Whom Here There
Let Make Get Find Check Draft Write Send Book Fix Add Remove Update Show Tell Give Help Need Want Try Run Use Look
Monday Tuesday Wednesday Thursday Friday Saturday Sunday Today Tomorrow Yesterday Tonight Morning Evening Night
January February March April May June July August September October November December Domo Person
Jag Du Han Hon Vi Ni De Den Det Detta Dessa Min Mitt Mina Din Ditt Dina Vår Vårt Våra Er Ert Era Hej Tack Ja Nej
Och Eller Men Om Så Att Är Var Vad När Hur Varför Vem Vilken Vilket Kan Ska Skulle Vill Måste Har Hade Blir Bli
Måndag Tisdag Onsdag Torsdag Fredag Lördag Söndag Idag Imorgon Igår Kolla Skriv Skicka Boka Fixa Hjälp Visa Gör
""".split())
WORD = r"[A-ZÅÄÖ][\wåäöÅÄÖ'&.-]*[\wåäö]"


class FilingError(Exception):
    pass


# ---- Jev ----------------------------------------------------------------------------------------

def jev_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def ask(state: str, questions: dict, *, timeout: float = 10.0) -> dict:
    """One Jev request: every question answered about the same state, in one round trip."""
    key = jev_key()
    if not key:
        raise FilingError("no TYPESAFE_API_KEY")
    url = os.environ.get("DOMO_JEV_URL", "https://api.typesafe.ai/v1").rstrip("/") + "/systemone"
    body = json.dumps({"model": os.environ.get("DOMO_JEV_MODEL", "jev-latest"), "state": state, "questions": questions}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("answers", {})
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FilingError(f"Jev didn't answer: {exc}") from exc


# ---- what a chat is about -------------------------------------------------------------------

def candidates(text: str, known: list[str], *, limit: int = 10) -> list[str]:
    """Names a chat keeps coming back to: known sub-spaces it mentions first, then proper nouns
    (one to three capitalised words, not sentence starters unless they recur), quoted names and repo
    or file-ish names. Most-mentioned first."""
    found: Counter[str] = Counter()
    # code and tool calls aren't what a chat is about: drop JSON objects and fenced code first
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\{[^{}]*\}", " ", re.sub(r"\[\[[^\]]*\]\]", " ", text))
    low = text.lower()
    for k in known:
        n = len(re.findall(rf"(?<![\w]){re.escape(k.lower())}(?![\w])", low))
        if n:
            found[k] += n + 2   # an existing sub-space wins ties
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        words = re.findall(rf"{WORD}(?:\s+{WORD}){{0,2}}", sentence)
        for i, phrase in enumerate(words):
            # possessives and Swedish compounds name the thing: Baltazar's, Baltazar-offsiten → Baltazar
            bare = (re.sub(r"-[a-zåäö][\wåäö]*$", "", re.sub(r"['’]s$", "", w)) for w in phrase.split())
            parts = [p for p in bare if p not in NOT_NAMES]
            if not parts:
                continue
            name = " ".join(parts)
            if len(name) < 3:
                continue
            starts = sentence.lstrip().startswith(phrase)
            found[name] += 0.5 if starts else 1
    for q in re.findall(r"[\"“'`]([^\"”'`\n]{3,40})[\"”'`]", text):
        if len(q.split()) <= 4:
            found[q.strip()] += 1
    for repo in re.findall(r"\b([a-z0-9]+(?:[-_][a-z0-9]+)+)\b", text):   # payments-retry, v2-work
        if not re.fullmatch(r"\d+([-_]\d+)*", repo):
            found[repo] += 1
    ranked = [n for n, c in found.most_common() if c >= 1]
    out: list[str] = []
    for n in ranked:
        if not any(n.lower() == o.lower() for o in out):
            out.append(n)
        if len(out) >= limit:
            break
    return out


def chat_text(home: Path, session_id: str) -> Optional[dict]:
    """Title, source, revision and the opening of a chat, from Hermes's own database."""
    db = search._hermes(home)
    if db is None:
        return None
    try:
        s = db.execute("SELECT id, title, source, COALESCE(transcript_revision, 0) AS rev FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if s is None:
            return None
        rows = db.execute("SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user','assistant') AND active = 1"
                          " AND COALESCE(content, '') != '' ORDER BY id LIMIT 8", (session_id,)).fetchall()
        lines = [f"{'Person' if r['role'] == 'user' else 'Assistant'}: {search.clean(r['content'])[:1200]}" for r in rows]
        users = sum(1 for r in rows if r["role"] == "user")
        return {"title": s["title"] or "", "source": s["source"] or "", "revision": int(s["rev"] or 0),
                "text": "\n".join(lines)[:MAX_TEXT], "turns": users}
    finally:
        db.close()


TOPIC_MIN = 0.75       # sure it's about an existing topic
MAX_TOPICS = 3
ASK_TOPICS = 12        # existing topics asked about per chat: the ones it names, then the most recent


def classify(store, title: str, text: str) -> Optional[dict]:
    """A chat's area and topics: {area, area_conf, topics: [{id|None, name, p}], reason}, or None."""
    spaces = store.spaces(include_quiet=True)
    areas = [s for s in spaces if s["kind"] == "area"]
    topics = [s for s in spaces if s["kind"] == "topic"]
    if not areas and not topics:
        return None
    body = f"{title}\n{text}"
    low = body.lower()
    names = {t["id"]: [t["name"], *store.aliases(t["id"])] for t in topics}
    said = lambda n: re.search(rf"(?<![\w]){re.escape(n.lower())}(?![\w])", low) is not None  # noqa: E731
    mentioned = [t for t in topics if any(said(n) for n in names[t["id"]])]
    asking = (mentioned + [t for t in topics if t not in mentioned])[:ASK_TOPICS]
    taken = {n.lower() for ns in names.values() for n in ns} | {a["name"].lower() for a in areas}
    fresh = [c for c in candidates(body, [n for ns in names.values() for n in ns]) if c.lower() not in taken][:5]
    questions: dict = {}
    if areas:
        crit = {a["id"]: (f"{a['name']}: {a['description']}" if a["description"] else a["name"])[:300] for a in areas}
        crit["none"] = "None of these, or it isn't clear from the chat"
        questions["area"] = {"type": "choice", "instructions": "Which part of the person's life is this chat about?", "criteria": crit}
    for i, t in enumerate(asking):
        about = f"{t['name']}" + (f" ({t['description']})" if t["description"] else "")
        questions[f"t{i}"] = {"type": "noul", "instructions": f"Is this chat about {about}?"}
    for i, c in enumerate(fresh):
        questions[f"n{i}"] = {"type": "noul", "instructions": f"Is this chat mainly about {c}, a specific project, client, person, place, trip or repo?"}
    answers = ask(f"Chat title: {title or '(untitled)'}\n\n{text}", questions)
    area, area_conf = None, 0.0
    a = answers.get("area") or {}
    if a.get("choice") in {x["id"] for x in areas} and float(a.get("confidence") or 0) >= SPACE_MIN:
        area, area_conf = a["choice"], float(a["confidence"])
    picked = []
    for i, t in enumerate(asking):
        p = float((answers.get(f"t{i}") or {}).get("noul") or 0)
        if p >= TOPIC_MIN:
            picked.append({"id": t["id"], "name": t["name"], "p": p})
    for i, c in enumerate(fresh):
        p = float((answers.get(f"n{i}") or {}).get("noul") or 0)
        times = len(re.findall(rf"(?<![\w]){re.escape(c)}(?![\w])", body))   # as a name: "Plan" ≠ "a plan"
        # a new topic only when no existing one fits, and for what the chat is plainly about: in its title,
        # or named more than once. Reuse first keeps the topics few as the chats pile up.
        if p >= NEW_MIN and not any(x["id"] for x in picked) and (times >= 2 or c.lower() in title.lower()):
            picked.append({"id": None, "name": c, "p": p})
    picked.sort(key=lambda x: (x["id"] is None, -x["p"]))   # what exists first, then the surest
    picked = picked[:MAX_TOPICS]
    if not area and not picked:
        return None
    named = [next(x["name"] for x in areas if x["id"] == area)] if area else []
    reason = " · ".join(named + [f"{x['name']}{' (new)' if x['id'] is None else ''}" for x in picked])
    return {"area": area, "area_conf": round(area_conf, 2), "topics": picked, "reason": reason}


# ---- tagging -----------------------------------------------------------------------------------

def file_session(home: Path, store, session_id: str) -> Optional[dict]:
    """Tag one chat with its area and topics; adds to what's there, never moves what the person set."""
    chat = chat_text(home, session_id)
    if not chat or chat["source"] in (*search.HIDDEN_SOURCES, "cron") or chat["title"].lower() in SKIP_TITLES or len(chat["text"]) < 12:
        return None
    if store.filing_attempts().get(session_id) == chat["revision"]:
        return None   # nothing new said since it was last looked at
    current = store.tags("session", session_id, include_off=True)
    if current and all(t["state"] == "off" for t in current):
        store.filing_attempted(session_id, chat["revision"])
        return None   # the person took its tags off: it stays as they left it
    try:
        d = classify(store, chat["title"], chat["text"])
    except FilingError as exc:
        log.info("tagging %s skipped: %s", session_id, exc)
        return None
    store.filing_attempted(session_id, chat["revision"])
    if not d:
        return None
    on = {t["space_id"] for t in current if t["state"] == "on"}
    has_area = any(t["kind"] == "area" and t["state"] == "on" for t in current)
    added, created = [], []
    if d["area"] and not has_area and store.tag("session", session_id, d["area"], by="domo", confidence=d["area_conf"], reason=d["reason"]):
        added.append(d["area"])
    home_area = d["area"] or next((t["space_id"] for t in current if t["kind"] == "area" and t["state"] == "on"), None)
    for t in d["topics"]:
        tid = t["id"]
        if tid is None:
            existing = store._space_named(t["name"], "topic")
            tid = existing["id"] if existing else store.create_space(t["name"], kind="topic", parent_id=home_area, origin="domo")["id"]
            if not existing:
                created.append(t["name"])
        if tid not in on and store.tag("session", session_id, tid, by="domo", confidence=round(t["p"], 2), reason=d["reason"]):
            added.append(tid)
    if not added:
        return None
    names = [store.space(x)["name"] for x in added if store.space(x)]
    store.note("filed", f"tagged “{chat['title'] or 'a chat'}” {', '.join(names)}", ref=session_id, space=names[0] if names else "")
    return {"session_id": session_id, "title": chat["title"], "tags": added, "where": " · ".join(names),
            "created": bool(created), "new_topics": created, "reason": d["reason"]}


def after_turn(home: Path, store, session_id: str) -> Optional[dict]:
    """Right after a chat's first turns: tag it while it's fresh. Later turns wait for the stitcher."""
    chat = chat_text(home, session_id)
    if not chat or chat["turns"] > 3:
        return None
    return file_session(home, store, session_id)


def sweep(home: Path, store, *, limit: int = 40, days: int = 30) -> list[dict]:
    """The stitcher's pass: chats from the last month with something new said since they were last looked at."""
    db = search._hermes(home)
    if db is None:
        return []
    try:
        hidden = ",".join("?" * len(search.HIDDEN_SOURCES))
        rows = db.execute(
            f"SELECT id, COALESCE(transcript_revision, 0) FROM sessions WHERE source NOT IN ({hidden}, 'cron') AND hidden = 0 AND COALESCE(archived, 0) = 0"
            " AND COALESCE(last_activity_at, started_at) > ? ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 400",
            (*search.HIDDEN_SOURCES, time.time() - days * 86400)).fetchall()
    finally:
        db.close()
    seen = store.filing_attempts()
    out = []
    for sid, rev in [r for r in rows if seen.get(r[0]) != r[1]][:limit]:   # at most `limit` looks per pass
        done = file_session(home, store, sid)
        if done:
            out.append(done)
    return out
