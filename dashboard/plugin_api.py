"""Domo app backend, mounted by the Hermes dashboard at /api/plugins/domo/.

The dashboard's login gate guards every route. Domo's own data (brief, decisions, to-dos, runs,
receipts, facts) is read from the profile's store; chat, sessions and routines are passed through
to that profile's API server over loopback.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the plugins dir, so `domo` imports as a package

from domo import bridge  # noqa: E402
from domo.store import Store, StoreError, store_path  # noqa: E402

log = logging.getLogger(__name__)
router = APIRouter()
VERSION = "0.1.0"
INBOX_TITLE = "Domo inbox"

_stores: dict[str, Store] = {}
transport = None  # tests inject an httpx transport for the API server


def _store(profile: str) -> Store:
    try:
        home = bridge.profile_home(profile)
    except bridge.ProfileError as exc:
        raise HTTPException(404, str(exc))
    path = str(store_path(home))
    if path not in _stores:
        _stores[path] = Store(Path(path))
    return _stores[path]


def _api(profile: str) -> bridge.ApiServer:
    try:
        return bridge.ApiServer(profile, transport=transport)
    except bridge.ProfileError as exc:
        raise HTTPException(404, str(exc))


def _store_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StoreError as exc:
        raise HTTPException(409 if "already" in str(exc) else 400, str(exc))


async def _passthrough(profile: str, method: str, path: str, *, body: Optional[dict] = None,
                       params: Optional[dict] = None):
    api = _api(profile)
    try:
        response = await api.request(method, path, json_body=body, params=params)
    except bridge.ProfileError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Hermes API server unreachable for {profile}: {exc}")
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text[:500])
    return response.json() if response.content else {}


# ---- meta -------------------------------------------------------------------------------------

@router.get("/health")
async def health():
    profiles = []
    for name in bridge.profile_names():
        entry = {**bridge.profile_info(name), "api_server": "unknown"}
        try:
            response = await _api(name).request("GET", "/health", timeout=3.0)
            entry["api_server"] = "ok" if response.status_code == 200 else f"http {response.status_code}"
        except Exception as exc:
            entry["api_server"] = f"unreachable: {type(exc).__name__}"
        profiles.append(entry)
    return {"domo": VERSION, "profiles": profiles}


@router.get("/profiles")
def profiles():
    return {"profiles": [bridge.profile_info(n) for n in bridge.profile_names()]}


# ---- Domo items -------------------------------------------------------------------------------

@router.get("/feed")
def feed(profile: str = Query(bridge.DEFAULT, alias="agent")):
    store = _store(profile)
    return {"profile": bridge.profile_info(profile), **store.feed()}


@router.get("/changes")
def changes(profile: str = Query(bridge.DEFAULT, alias="agent"), since: int = Query(0, ge=0)):
    items, cursor = _store(profile).changed_since(since)
    return {"items": items, "cursor": cursor}


@router.get("/items")
def items(profile: str = Query(bridge.DEFAULT, alias="agent"), kind: Optional[str] = None, status: Optional[str] = None,
          space: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    return {"items": _store(profile).list(kind, status=status, space=space, limit=limit)}


class TodoIn(BaseModel):
    title: str
    body: str = ""
    space: str = ""
    owner: str = Field("me", pattern="^(me|domo)$")


@router.post("/todos")
def add_todo(todo: TodoIn, profile: str = Query(bridge.DEFAULT, alias="agent")):
    return _store_call(_store(profile).create, "todo", todo.title, body=todo.body, space=todo.space,
                       data={"owner": todo.owner})


class ItemPatch(BaseModel):
    status: str = Field(..., pattern="^(open|done|dismissed)$")


@router.patch("/items/{item_id}")
def patch_item(item_id: str, patch: ItemPatch, profile: str = Query(bridge.DEFAULT, alias="agent")):
    store = _store(profile)
    item = _store_call(store.get, item_id)
    if item["kind"] not in ("todo", "receipt"):
        raise HTTPException(400, "only to-dos and receipts change status here; answer decisions instead")
    return _store_call(store.update, item_id, status=patch.status)


class Answer(BaseModel):
    choice: str
    note: str = ""
    by: str = ""


@router.post("/decisions/{decision_id}/answer")
def answer(decision_id: str, body: Answer, background: BackgroundTasks, profile: str = Query(bridge.DEFAULT, alias="agent")):
    store = _store(profile)
    item = _store_call(store.answer, decision_id, body.choice, note=body.note, by=body.by)
    item = store.update(decision_id, data={"resume": "queued"})
    background.add_task(_resume_agent, profile, decision_id)
    return item


async def _resume_agent(profile: str, decision_id: str) -> None:
    """Tell the agent the answer in its Domo inbox session, so parked work continues."""
    store = _store(profile)
    item = store.get(decision_id)
    message = (f"[Domo] Decision answered: \"{item['title']}\" → {item['data'].get('choice')}."
               + (f" Note: {item['data']['note']}." if item["data"].get("note") else "")
               + (f" It was asked in session {item['session_id']}." if item["session_id"] else "")
               + " Continue the work accordingly and record a receipt when done.")
    try:
        session_id = await _inbox_session(profile, store)
        response = await _api(profile).request("POST", f"/api/sessions/{session_id}/chat",
                                               json_body={"message": message}, timeout=600.0)
        state = "sent" if response.status_code < 400 else f"failed: http {response.status_code}"
    except Exception as exc:
        log.warning("domo: resuming %s for %s failed: %s", decision_id, profile, exc)
        state = f"failed: {type(exc).__name__}"
    store.update(decision_id, data={"resume": state})


async def _inbox_session(profile: str, store: Store) -> str:
    marker = store.path.parent / "inbox_session"
    if marker.exists():
        return marker.read_text().strip()
    api = _api(profile)
    response = await api.request("POST", "/api/sessions", json_body={"title": INBOX_TITLE})
    if response.status_code < 400:
        data = response.json()
        session_id = data.get("id") or data.get("session_id") or (data.get("session") or {}).get("id")
    else:  # titles are unique per profile: the inbox already exists (the marker file was lost)
        listing = (await api.request("GET", "/api/sessions", params={"limit": 200})).json()
        rows = listing.get("data") or listing.get("sessions") or []
        session_id = next((r["id"] for r in rows if r.get("title") == INBOX_TITLE), None)
    if not session_id:
        raise RuntimeError(f"could not create or find the {INBOX_TITLE!r} session")
    marker.write_text(session_id)
    return session_id


@router.get("/facts")
def facts(profile: str = Query(bridge.DEFAULT, alias="agent")):
    return {"facts": _store(profile).facts()}


# ---- chat, sessions and routines (passed through to the API server) ----------------------------

@router.get("/sessions")
async def sessions(profile: str = Query(bridge.DEFAULT, alias="agent"), limit: int = Query(30, ge=1, le=200)):
    return await _passthrough(profile, "GET", "/api/sessions", params={"limit": limit})


class NewSession(BaseModel):
    title: str = ""


@router.post("/sessions")
async def new_session(body: NewSession, profile: str = Query(bridge.DEFAULT, alias="agent")):
    return await _passthrough(profile, "POST", "/api/sessions", body={"title": body.title} if body.title else {})


@router.get("/sessions/{session_id}/messages")
async def messages(session_id: str, profile: str = Query(bridge.DEFAULT, alias="agent")):
    return await _passthrough(profile, "GET", f"/api/sessions/{session_id}/messages",
                              params={"inline_images": "false"})


class ChatIn(BaseModel):
    message: str


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, body: ChatIn, profile: str = Query(bridge.DEFAULT, alias="agent")):
    api = _api(profile)
    return StreamingResponse(api.stream(f"/api/sessions/{session_id}/chat/stream", {"message": body.message}),
                             media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class ApprovalIn(BaseModel):
    request_id: str
    execution_generation: Optional[int] = None
    choice: str


@router.post("/runs/{run_id}/approval")
async def run_approval(run_id: str, body: ApprovalIn, profile: str = Query(bridge.DEFAULT, alias="agent")):
    return await _passthrough(profile, "POST", f"/v1/runs/{run_id}/approval", body=body.model_dump(exclude_none=True))


@router.get("/jobs")
async def jobs(profile: str = Query(bridge.DEFAULT, alias="agent")):
    return await _passthrough(profile, "GET", "/api/jobs")


@router.post("/jobs/{job_id}/{action}")
async def job_action(job_id: str, action: str, profile: str = Query(bridge.DEFAULT, alias="agent")):
    if action not in ("run", "pause", "resume"):
        raise HTTPException(404, "unknown action")
    return await _passthrough(profile, "POST", f"/api/jobs/{job_id}/{action}")
