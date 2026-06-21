"""
Dog bathroom tracker — FastAPI app.

Pages:
  GET  /              -> redirect to /log (temporary)
  GET  /log           -> phone logging page (toggle log/unlog, per-button state)
  GET  /display       -> wall display for TRMNL (header-gated, query-time grouping)
  GET  /advanced      -> recent-entry list: delete, accident logging, notes

API:
  POST   /api/toggle         -> log or un-log a pee/poop (main page button behavior)
  POST   /api/log            -> raw event insert (physical buttons / accident logging)
  GET    /api/events         -> recent raw events (for the Advanced list)
  DELETE /api/event/{id}     -> delete one event by id
  PATCH  /api/event/{id}     -> edit notes / location on one event
  POST   /api/prune          -> retention prune (cron)
  GET    /healthz            -> health check

Security posture: user-supplied values reaching SQL are constrained to
allow-lists (dog name), a 3-value enum (location), and integer ids. All DB
access is parameterized. Column names interpolated into SQL come only from
internal constants ('pee'/'poop'), never from request data.
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/app/data/log.db")
TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR", "/app/templates")
TZ_NAME = os.environ.get("TZ", "America/Denver")
LOCAL_TZ = ZoneInfo(TZ_NAME)

DISPLAY_TOKEN = os.environ.get("DISPLAY_TOKEN", "")
DISPLAY_HEADER = os.environ.get("DISPLAY_HEADER", "X-Display-Token")

DOGS = ["buddy", "crystal", "harper"]
LOCATIONS = {"outside", "inside"}
KINDS = ("pee", "poop")

GROUP_WINDOW_SECONDS = int(os.environ.get("GROUP_WINDOW_SECONDS", "1500"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
DISPLAY_ROWS = int(os.environ.get("DISPLAY_ROWS", "5"))
ADVANCED_ROWS = int(os.environ.get("ADVANCED_ROWS", "30"))

app = FastAPI(title="Dog Bathroom Tracker")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                dog       TEXT    NOT NULL,
                pee       TEXT,
                poop      TEXT,
                notes     TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp)")


@app.on_event("startup")
def _startup():
    init_db()


# ---------------------------------------------------------------------------
# Validation — the security boundary.
# ---------------------------------------------------------------------------

def validate_dog(value) -> str:
    if not isinstance(value, str) or value.lower() not in DOGS:
        raise ValueError(f"dog must be one of {DOGS}")
    return value.lower()


def validate_location(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value.lower() not in LOCATIONS:
        raise ValueError(f"location must be null or one of {sorted(LOCATIONS)}")
    return value.lower()


def validate_kind(value) -> str:
    if value not in KINDS:
        raise ValueError("kind must be 'pee' or 'poop'")
    return value


def validate_timestamp(value, now_epoch):
    """Optional caller-supplied epoch for backdated entries. Absent -> now.
    Bounds are loose on purpose: reject the future (60s skew grace) and
    anything older than the retention window (it'd be pruned immediately)."""
    if value is None or value == "":
        return now_epoch
    try:
        ts = int(value)
    except (TypeError, ValueError):
        raise ValueError("timestamp must be an integer epoch")
    if ts > now_epoch + 60:
        raise ValueError("timestamp cannot be in the future")
    if ts < now_epoch - RETENTION_DAYS * 86400:
        raise ValueError("timestamp is older than the retention window")
    return ts


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def humanize_age(then_epoch: int, now_epoch: int) -> str:
    delta = max(0, now_epoch - then_epoch)
    mins = delta // 60
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def format_row_time(epoch: int, now: datetime) -> str:
    dt = datetime.fromtimestamp(epoch, LOCAL_TZ)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%a %H:%M")


def format_full_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, LOCAL_TZ).strftime("%a %b %d, %H:%M")


# ---------------------------------------------------------------------------
# Query-time grouping
# ---------------------------------------------------------------------------

def cell_value(events_for_dog, kind):
    seen_outside = False
    for ev in events_for_dog:
        v = ev[kind]
        if v == "inside":
            return "inside"
        if v == "outside":
            seen_outside = True
    return "outside" if seen_outside else None


def group_events(rows, now_epoch):
    slots = []
    current = None
    anchor_ts = None
    for r in rows:
        ts = r["timestamp"]
        if current is None or (anchor_ts - ts) > GROUP_WINDOW_SECONDS:
            current = {"anchor_ts": ts, "events": []}
            anchor_ts = ts
            slots.append(current)
        current["events"].append(r)

    out = []
    for slot in slots:
        by_dog = {d: [] for d in DOGS}
        has_note = False
        for ev in slot["events"]:
            by_dog[ev["dog"]].append(ev)
            if ev["notes"]:
                has_note = True
        out.append({
            "anchor_ts": slot["anchor_ts"],
            "has_note": has_note,
            "cells": {
                d: {"pee": cell_value(by_dog[d], "pee"),
                    "poop": cell_value(by_dog[d], "poop")}
                for d in DOGS
            },
        })
    return out


def latest_per_dog(rows, now_epoch):
    out = {d: {"pee": None, "poop": None} for d in DOGS}
    for r in rows:
        d = r["dog"]
        if r["pee"] and out[d]["pee"] is None:
            out[d]["pee"] = humanize_age(r["timestamp"], now_epoch)
        if r["poop"] and out[d]["poop"] is None:
            out[d]["poop"] = humanize_age(r["timestamp"], now_epoch)
    return out


def recent_event_in_window(conn, dog, kind, now_epoch):
    """Most recent event for dog where `kind` is set, within the window, else None.
    `kind` is validated against KINDS before reaching here, so interpolating it
    into the SQL is safe (never request-derived free text)."""
    cutoff = now_epoch - GROUP_WINDOW_SECONDS
    return conn.execute(
        f"SELECT * FROM events WHERE dog = ? AND {kind} IS NOT NULL "
        "AND timestamp >= ? ORDER BY timestamp DESC LIMIT 1",
        (dog, cutoff),
    ).fetchone()


def compute_button_state(conn, dog, kind, now_epoch):
    """State for one dog+kind button. Used by both page load and toggle response
    so the button looks identical however it got there."""
    hist = conn.execute(
        f"SELECT timestamp FROM events WHERE dog = ? AND {kind} IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1",
        (dog,),
    ).fetchone()
    age = humanize_age(hist["timestamp"], now_epoch) if hist else None
    recent = recent_event_in_window(conn, dog, kind, now_epoch)
    if recent is not None:
        logged = datetime.fromtimestamp(recent["timestamp"], LOCAL_TZ).strftime("%H:%M")
        return {"state": "undo", "logged_at": logged, "age": age}
    return {"state": "fresh", "age": age}


def button_states(now_epoch):
    """Per dog+kind button state for the whole /log page."""
    states = {d: {} for d in DOGS}
    with get_db() as conn:
        for d in DOGS:
            for kind in KINDS:
                states[d][kind] = compute_button_state(conn, d, kind, now_epoch)
    return states


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return RedirectResponse(url="/log", status_code=307)


@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request):
    now_epoch = int(time.time())
    states = button_states(now_epoch)
    now_local = datetime.fromtimestamp(now_epoch, LOCAL_TZ)
    return templates.TemplateResponse(
        request, "log.html",
        {"dogs": DOGS, "states": states,
         "now_str": now_local.strftime("%a %H:%M"),
         "window": GROUP_WINDOW_SECONDS},
    )


@app.get("/display", response_class=HTMLResponse)
async def display_page(request: Request):
    if DISPLAY_TOKEN:
        if request.headers.get(DISPLAY_HEADER, "") != DISPLAY_TOKEN:
            return Response("not found", status_code=404)

    now_epoch = int(time.time())
    cutoff = now_epoch - RETENTION_DAYS * 86400
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp >= ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()

    now_local = datetime.fromtimestamp(now_epoch, LOCAL_TZ)
    slots = group_events(rows, now_epoch)[:DISPLAY_ROWS]
    ages = latest_per_dog(rows, now_epoch)
    for s in slots:
        s["time_str"] = format_row_time(s["anchor_ts"], now_local)

    return templates.TemplateResponse(
        request, "display.html",
        {"dogs": DOGS, "slots": slots, "ages": ages,
         "fetched_str": now_local.strftime("%a %H:%M")},
    )


@app.get("/advanced", response_class=HTMLResponse)
async def advanced_page(request: Request):
    now_epoch = int(time.time())
    cutoff = now_epoch - RETENTION_DAYS * 86400
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (cutoff, ADVANCED_ROWS),
        ).fetchall()

    events = [{
        "id": r["id"], "dog": r["dog"], "dog_disp": r["dog"].capitalize(),
        "pee": r["pee"], "poop": r["poop"], "notes": r["notes"] or "",
        "when": format_full_time(r["timestamp"]),
    } for r in rows]

    return templates.TemplateResponse(
        request, "advanced.html", {"dogs": DOGS, "events": events},
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/toggle")
async def api_toggle(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        dog = validate_dog(body.get("dog"))
        kind = validate_kind(body.get("kind"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    now_epoch = int(time.time())
    with get_db() as conn:
        recent = recent_event_in_window(conn, dog, kind, now_epoch)
        if recent is not None:
            other = "poop" if kind == "pee" else "pee"
            if recent[other] is None and not recent["notes"]:
                conn.execute("DELETE FROM events WHERE id = ?", (recent["id"],))
            else:
                conn.execute(f"UPDATE events SET {kind} = NULL WHERE id = ?", (recent["id"],))
            action = "removed"
        else:
            conn.execute(
                f"INSERT INTO events (timestamp, dog, {kind}) VALUES (?, ?, ?)",
                (now_epoch, dog, "outside"),
            )
            action = "logged"
        # Recompute the button's state AFTER the change so the client can render
        # it identically to a fresh page load (handles revert-to-historical and
        # the double-log-in-window edge for free).
        state = compute_button_state(conn, dog, kind, now_epoch)

    return {"ok": True, "action": action, "dog": dog, "kind": kind, "button": state}


@app.post("/api/log")
async def api_log(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        dog = validate_dog(body.get("dog"))
        pee = validate_location(body.get("pee"))
        poop = validate_location(body.get("poop"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if pee is None and poop is None:
        return JSONResponse({"error": "at least one of pee or poop must be set"}, status_code=400)

    notes = body.get("notes")
    if notes is not None and not isinstance(notes, str):
        return JSONResponse({"error": "notes must be a string"}, status_code=400)
    if isinstance(notes, str):
        notes = notes[:500] or None

    now_epoch = int(time.time())
    try:
        ts = validate_timestamp(body.get("timestamp"), now_epoch)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO events (timestamp, dog, pee, poop, notes) VALUES (?, ?, ?, ?, ?)",
            (ts, dog, pee, poop, notes),
        )
        new_id = cur.lastrowid
    return {"ok": True, "id": new_id, "timestamp": ts}


@app.get("/api/events")
async def api_events():
    now_epoch = int(time.time())
    cutoff = now_epoch - RETENTION_DAYS * 86400
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (cutoff, ADVANCED_ROWS),
        ).fetchall()
    return {"ok": True, "events": [{
        "id": r["id"], "timestamp": r["timestamp"], "dog": r["dog"],
        "pee": r["pee"], "poop": r["poop"], "notes": r["notes"],
        "when": format_full_time(r["timestamp"]),
    } for r in rows]}


@app.delete("/api/event/{event_id}")
async def api_delete_event(event_id: int):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        if cur.rowcount == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "deleted": event_id}


@app.patch("/api/event/{event_id}")
async def api_patch_event(event_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    sets, params = [], []
    for col in ("pee", "poop"):
        if col in body:
            try:
                params.append(validate_location(body[col]))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            sets.append(f"{col} = ?")
    if "notes" in body:
        n = body["notes"]
        if n is not None and not isinstance(n, str):
            return JSONResponse({"error": "notes must be a string or null"}, status_code=400)
        if isinstance(n, str):
            n = n[:500] or None
        sets.append("notes = ?"); params.append(n)

    if not sets:
        return JSONResponse({"error": "no editable fields provided"}, status_code=400)

    params.append(event_id)
    with get_db() as conn:
        cur = conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "updated": event_id}


@app.post("/api/prune")
async def api_prune():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with get_db() as conn:
        cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        deleted = cur.rowcount
    return {"ok": True, "deleted": deleted}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
