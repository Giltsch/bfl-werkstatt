"""Live sync for the Werkstatt: one poll (every 3 s with a project open, 10 s on the dashboard) carries the doc when it
changed, who else has the project open, and the GPU queue."""

import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

import projects

router = APIRouter()
VERSION = "3.0"
started = datetime.now().isoformat(timespec="seconds")   # set by the server at startup; clients reload when it changes
queue_view = lambda owner, project: {}                    # set by the server: scheduler.view
PRESENCE: dict[str, dict] = {}  # device -> {user, name, color, device_label, project, view, seen, since}
TTL, MAX_DEVICES = 12, 200
DEVICES = ("Tablet", "Handy", "PC")
clock = time.time
_LOCK = threading.Lock()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _presence(request: Request, project: str | None, view: str, label: str) -> list[dict]:
    dev = getattr(request.state, "device", None) or request.cookies.get("ws_dev") or "anon"
    me = getattr(request.state, "user", None)
    owner = getattr(request.state, "owner", None) or f"dev:{dev[:8]}"
    t = clock()
    with _LOCK:
        for d in [d for d, e in PRESENCE.items() if e["seen"] < t - TTL]:
            del PRESENCE[d]
        if not project:
            PRESENCE.pop(dev, None)
            return []
        old = PRESENCE.get(dev)
        same = old and old["project"] == project and old["view"] == view
        PRESENCE[dev] = {"user": me["id"] if me else owner,
                         "name": me["name"] if me else (getattr(request.state, "owner_name", None) or projects.name_of(owner)),
                         "color": me["color"] if me else "#9aa0a6", "device_label": label, "project": project,
                         "view": view, "seen": t, "since": old["since"] if same else t}
        if len(PRESENCE) > MAX_DEVICES:
            for d in sorted(PRESENCE, key=lambda d: PRESENCE[d]["seen"])[:len(PRESENCE) - MAX_DEVICES]:
                del PRESENCE[d]
        return [{"user": e["user"], "name": e["name"], "color": e["color"], "device": e["device_label"],
                 "view": e["view"], "since": _iso(e["since"])}
                for d, e in sorted(PRESENCE.items(), key=lambda kv: kv[1]["since"]) if d != dev and e["project"] == project]


@router.get("/api/sync")
def sync(request: Request, project: str | None = None, rev: int = 0, view: str = "", device: str = ""):
    view = view if view.isascii() and view.isalnum() and len(view) <= 20 else ""
    label = device if device in DEVICES else "PC"
    owner = getattr(request.state, "owner", None) or "anon"
    out = {"t": datetime.now().isoformat(timespec="seconds"), "server": {"started": started, "version": VERSION},
           "me": getattr(request.state, "user", None), "list_rev": projects.list_rev(), "project": None}
    try:
        out["news"] = projects.new_approvals()
    except Exception as e:
        print("sync: news failed:", e, flush=True)
    if project:
        try:
            doc = projects.load("workshop", project)
            out["project"] = {"rev": doc["rev"], "doc": doc if doc["rev"] > rev else None, "deleted": False}
        except HTTPException as e:
            if e.status_code != 404:
                raise
            out["project"] = {"rev": 0, "doc": None, "deleted": True}
            project = None
    out["presence"] = _presence(request, project, view, label)
    try:
        out["queue"] = queue_view(owner, project) or {}
    except Exception as e:  # a queue problem must not break editing
        print("sync: queue_view failed:", e, flush=True)
        out["queue"] = {}
    return out
