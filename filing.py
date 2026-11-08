"""Filing: every chat in its place without being asked (Work › Baltazar, Coding › Domo, Personal › Resor).

The person just starts chatting. After a chat's first turn, and again in the "Domo filing" routine for
anything left over, Domo asks Jev (TypeSafe's decision model: typed answers with calibrated confidence,
in a few hundred milliseconds, for a fraction of a cent) two questions about it:

- which space is this about (one of the person's top-level spaces, or none)?
- what, inside it, is it about: one of the existing sub-spaces, a named thing the chat keeps coming
  back to (a client, a repo, a trip), or nothing specific?

Jev chooses; it doesn't write. So the names a new sub-space can take come from the chat itself
(``candidates``: proper nouns, quoted names, repo names), and Jev picks among them. Code, not the model,
owns the thresholds: below them a chat is filed one level up, or left unfiled. Anything the person filed,
moved or unfiled by hand is never touched (store.tag_session).
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

SPACE_MIN = 0.6        # sure enough of the space to file it there
SUB_MIN = 0.6          # sure enough of an existing sub-space
NEW_MIN = 0.75         # sure enough of a new one to make it
SPECIFIC_MIN = 0.7     # sure the chat is about one named thing at all
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
        lines = [f"{'Person' if r['role'] == 'user' else 'Domo'}: {search.clean(r['content'])[:1200]}" for r in rows]
        users = sum(1 for r in rows if r["role"] == "user")
        return {"title": s["title"] or "", "source": s["source"] or "", "revision": int(s["rev"] or 0),
                "text": "\n".join(lines)[:MAX_TEXT], "turns": users}
    finally:
        db.close()


def classify(store, title: str, text: str) -> Optional[dict]:
    """Where a chat goes: {space_id, new_sub (a name to create under it) or None, confidence, reason}, or None."""
    tops = [s for s in store.spaces() if not s.get("parent_id")]
    if not tops:
        return None
    kids = [s for s in store.spaces() if s.get("parent_id")]
    names = {s["id"]: s["name"] for s in tops}
    fresh = [c for c in candidates(f"{title}\n{text}", [k["name"] for k in kids])
             if not any(c.lower() == k["name"].lower() for k in kids) and c.lower() not in {n.lower() for n in names.values()}][:8]
    space_q = {s["id"]: (f"{s['name']}: {s['description']}" if s["description"] else s["name"])[:300] for s in tops}
    space_q["none"] = "None of these, or it isn't clear from the chat"
    sub_q = {f"s{i}": f"{names.get(k['parent_id'], '')} › {k['name']}" + (f": {k['description']}" if k["description"] else "")
             for i, k in enumerate(kids)}
    sub_q.update({f"n{i}": c for i, c in enumerate(fresh)})
    sub_q["none"] = "No one specific project, client, person, place, trip or repo"
    state = f"Chat title: {title or '(untitled)'}\n\n{text}"
    answers = ask(state, {
        "space": {"type": "choice", "instructions": "Which part of the person's life is this chat about?", "criteria": space_q},
        "subject": {"type": "choice", "instructions": "Which project, client, person, place, trip or repo is the chat mainly about?", "criteria": sub_q},
        "specific": {"type": "noul", "instructions": "Is the chat mainly about one specific, named project, client, person, place, trip or repo?"},
    })
    space = answers.get("space", {})
    space_id, sure = space.get("choice"), float(space.get("confidence") or 0)
    if space_id not in names or sure < SPACE_MIN:
        return None
    subject = answers.get("subject", {})
    pick, sub_sure = subject.get("choice") or "none", float(subject.get("confidence") or 0)
    specific = float((answers.get("specific") or {}).get("noul") or 0)
    if pick.startswith("s") and pick[1:].isdigit():
        kid = kids[int(pick[1:])]
        if kid["parent_id"] == space_id and sub_sure >= SUB_MIN:
            return {"space_id": kid["id"], "new_sub": None, "confidence": round(min(sure, sub_sure), 2),
                    "reason": f"about {kid['name']} ({round(sub_sure * 100)}% sure)"}
    if pick.startswith("n") and pick[1:].isdigit():
        name = fresh[int(pick[1:])]
        mentioned = len(re.findall(re.escape(name.lower()), f"{title}\n{text}".lower()))
        # a new sub-space only for something the chat is plainly about: named in its title, or more than once
        if sub_sure >= NEW_MIN and specific >= SPECIFIC_MIN and (mentioned >= 2 or name.lower() in title.lower()):
            return {"space_id": space_id, "new_sub": name, "confidence": round(min(sure, sub_sure, specific), 2),
                    "reason": f"about {name}, new ({round(sub_sure * 100)}% sure)"}
    return {"space_id": space_id, "new_sub": None, "confidence": round(sure, 2), "reason": f"{names[space_id]} ({round(sure * 100)}% sure)"}


# ---- filing ------------------------------------------------------------------------------------

def file_session(home: Path, store, session_id: str) -> Optional[dict]:
    """File one chat if no one has: returns what was done, or None when it stays where it is."""
    current = store.filings().get(session_id)
    if current and (current["filed_by"] == "person" or current["space_id"]):
        return None
    chat = chat_text(home, session_id)
    if not chat or chat["source"] in (*search.HIDDEN_SOURCES, "cron") or chat["title"].lower() in SKIP_TITLES or len(chat["text"]) < 12:
        return None
    if store.filing_attempts().get(session_id) == chat["revision"]:
        return None   # nothing new said since it was last looked at
    try:
        d = classify(store, chat["title"], chat["text"])
    except FilingError as exc:
        log.info("filing %s skipped: %s", session_id, exc)
        return None
    store.filing_attempted(session_id, chat["revision"])
    if not d:
        return None
    space_id = d["space_id"]
    if d["new_sub"]:
        existing = next((s for s in store.children(space_id) if s["name"].lower() == d["new_sub"].lower()), None)
        space_id = (existing or store.create_space(d["new_sub"], parent_id=space_id, origin="domo"))["id"]
    filed = store.tag_session(session_id, space_id, by="domo", confidence=d["confidence"], reason=d["reason"])
    if filed != space_id:
        return None
    where = " › ".join(s["name"] for s in store.lineage(space_id))
    store.note("filed", f"filed “{chat['title'] or 'a chat'}” under {where}", ref=session_id, space=where)
    return {"session_id": session_id, "title": chat["title"], "space_id": space_id, "where": where,
            "created": bool(d["new_sub"]), **{k: d[k] for k in ("confidence", "reason")}}


def after_turn(home: Path, store, session_id: str) -> Optional[dict]:
    """Right after a chat's first turns: file it while it's fresh. Later turns wait for the routine."""
    chat = chat_text(home, session_id)
    if not chat or chat["turns"] > 3:
        return None
    return file_session(home, store, session_id)


def sweep(home: Path, store, *, limit: int = 40, days: int = 30) -> list[dict]:
    """The routine's pass: chats from the last month no one has filed and that have something new."""
    db = search._hermes(home)
    if db is None:
        return []
    try:
        hidden = ",".join("?" * len(search.HIDDEN_SOURCES))
        ids = [r[0] for r in db.execute(
            f"SELECT id FROM sessions WHERE source NOT IN ({hidden}, 'cron') AND hidden = 0 AND COALESCE(archived, 0) = 0"
            " AND COALESCE(last_activity_at, started_at) > ? ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 400",
            (*search.HIDDEN_SOURCES, time.time() - days * 86400))]
    finally:
        db.close()
    filings = store.filings()
    out = []
    for sid in [i for i in ids if i not in filings][:limit]:   # at most `limit` looks per pass
        r = file_session(home, store, sid)   # unchanged since its last look: skipped without asking Jev
        if r:
            out.append(r)
    return out
