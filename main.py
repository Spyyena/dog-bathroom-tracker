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

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import time
from urllib.parse import quote
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/app/data/log.db")
TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR", "/app/templates")
STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
TZ_NAME = os.environ.get("TZ", "America/Denver")
LOCAL_TZ = ZoneInfo(TZ_NAME)

DISPLAY_TOKEN = os.environ.get("DISPLAY_TOKEN", "")
DISPLAY_HEADER = os.environ.get("DISPLAY_HEADER", "X-Display-Token")
# When true, /display logs incoming request headers (token redacted) for
# diagnosing the fetcher (e.g. TRMNL). Off by default. Toggle in compose.
DISPLAY_DEBUG = os.environ.get("DISPLAY_DEBUG", "").lower() in ("1", "true", "yes", "on")

DOGS = ["buddy", "crystal", "harper"]
DOG_SENTINEL = "all"                      # standalone note applying to every dog
LOCATIONS = {"outside", "inside"}
KINDS = ("pee", "poop", "park")          # togglable buttons on /log
EVENT_COLS = ("pee", "poop", "park")     # columns that mean "something happened"

# NOTE: every os.environ.get(...) default below is used ONLY if the matching
# environment variable is unset. If a value is set in the compose environment:
# block (or a .env), THAT wins and these defaults are ignored. When changing a
# runtime value, check the compose block first — it may be overriding this.

GROUP_WINDOW_SECONDS = int(os.environ.get("GROUP_WINDOW_SECONDS", "1200"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
DISPLAY_ROWS = int(os.environ.get("DISPLAY_ROWS", "5"))
# Max seconds the cached display may go without a re-render even if no event is
# logged, so the relative "X ago" ages on the panel never drift further than
# this from truth. Forces a new ETag at least this often. Default 2h.
DISPLAY_MAX_STALE_SECONDS = int(os.environ.get("DISPLAY_MAX_STALE_SECONDS", "7200"))
ADVANCED_ROWS = int(os.environ.get("ADVANCED_ROWS", "30"))

# Minimum seconds between bingo celebrations. Stops a curious partner from
# undo/redo-cycling the last button to binge every animation. 0 disables the
# cooldown (every completing tap fires). In-memory; resets on container restart.
BINGO_COOLDOWN_SECONDS = int(os.environ.get("BINGO_COOLDOWN_SECONDS", "3600"))

# --- Auth -------------------------------------------------------------------
# USERS: comma-separated name:hash pairs. Generate a hash with
#   docker compose exec dog-tracker python main.py hashpw
# Auth is enforced only when USERS is set; unset = the app is open, as before.
USERS_RAW = os.environ.get("USERS", "")
# Signs session cookies. Rotating it signs out every device. If unset, a random
# one is generated at startup, which signs everyone out on every restart.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# How long a device stays signed in WITHOUT being used. Each authenticated
# request older than SESSION_RENEW_SECONDS quietly re-issues the cookie, so a
# device in regular use never sees the login page again.
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "365"))
SESSION_RENEW_SECONDS = int(os.environ.get("SESSION_RENEW_SECONDS", "86400"))
SESSION_COOKIE = "dt_session"
# Bearer token for Home Assistant / scripts: Authorization: Bearer <token>.
# Full API access; writes are recorded as logged_by='ha'.
API_TOKEN = os.environ.get("API_TOKEN", "")

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
                park      TEXT,
                notes     TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp)")
        _migrate_columns(conn)


# Columns the app expects, beyond the original schema. On an existing DB that
# predates a column, ADD it (nullable, so existing rows get NULL — correct: an
# old event simply wasn't a park visit / had no note). Non-destructive; keeps
# history across upgrades. Add future columns to this dict.
EXPECTED_COLUMNS = {
    "park": "TEXT",
    "notes": "TEXT",
    "logged_by": "TEXT",   # username, or 'ha' for the API token. Not displayed.
}


def _migrate_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    for col, coltype in EXPECTED_COLUMNS.items():
        if col not in existing:
            # col/coltype are internal constants, never request data.
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {coltype}")


@asynccontextmanager
async def _lifespan(app):
    # Replaces the deprecated @app.on_event("startup") pattern.
    init_db()
    yield


app = FastAPI(title="Dog Bathroom Tracker", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Auth: password hashing, signed session cookies, and the gatekeeper
# ---------------------------------------------------------------------------

_SCRYPT = (16384, 8, 1)   # n, r, p


def hash_password(pw: str) -> str:
    """scrypt.<n>.<r>.<p>.<salt hex>.<hash hex> — no '$' or ':' so it is safe
    unquoted in a .env and as a USERS value."""
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt.{n}.{r}.{p}.{salt.hex()}.{dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, want = stored.split(".")
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


def _parse_users(raw: str) -> dict:
    users = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, h = part.partition(":")
        name, h = name.strip().lower(), h.strip()
        if name and h.startswith("scrypt."):
            users[name] = h
        else:
            print(f"[auth] ignoring malformed USERS entry for '{name}'", file=sys.stderr, flush=True)
    return users


USERS = _parse_users(USERS_RAW)
AUTH_ENABLED = bool(USERS)
if AUTH_ENABLED and not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("[auth] SESSION_SECRET unset; using a random one (sessions end on restart)",
          file=sys.stderr, flush=True)
# Hashed against when the username is unknown, so a miss costs the same time
# as a wrong password (no username probing by timing).
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _sig(user: str, payload: str) -> str:
    # Keyed by the secret AND the user's stored hash: changing one person's
    # password signs out only that person's devices.
    key = (SESSION_SECRET + "|" + USERS.get(user, "")).encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session(user: str) -> str:
    payload = f"{user}|{int(time.time())}"
    b = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b}.{_sig(user, payload)}"


def read_session(cookie: str):
    """Return (user, issued_epoch) for a valid, unexpired cookie, else None."""
    try:
        b, sig = cookie.split(".", 1)
        payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
        user, issued = payload.split("|", 1)
        issued = int(issued)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if user not in USERS:                      # removed from .env = signed out
        return None
    if not hmac.compare_digest(sig, _sig(user, payload)):
        return None
    now = int(time.time())
    if issued > now + 60 or now - issued > SESSION_DAYS * 86400:
        return None
    return user, issued


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", "").lower() == "https"
            or request.url.scheme == "https")


def set_session_cookie(resp: Response, request: Request, user: str):
    resp.set_cookie(SESSION_COOKIE, make_session(user), max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=_is_https(request), path="/")


def _safe_next(nxt) -> str:
    """Only allow local paths, so /login?next= can't bounce people off-site."""
    if isinstance(nxt, str) and nxt.startswith("/") and not nxt.startswith("//") \
            and "\\" not in nxt:
        return nxt
    return "/log"


# Reachable with no credentials at all.
_OPEN_PATHS = {"/login", "/logout", "/healthz", "/favicon.ico", "/favicon.svg",
               "/favicon-16.png", "/favicon-32.png", "/apple-touch-icon.png",
               "/apple-touch-icon-precomposed.png", "/apple-touch-icon-152.png",
               "/apple-touch-icon-120.png"}


class AuthGate(BaseHTTPMiddleware):
    """Auth-by-default. Identifies the caller as a session user, 'ha' (API
    bearer token) or 'display' (TRMNL's header token), then decides.
      browsers, unauthenticated GET page -> redirect to /login
      anything else unauthenticated      -> 401 JSON
      /display: display token OR a session; a wrong token -> 404, as before
    """
    async def dispatch(self, request, call_next):
        path = request.url.path
        request.state.user = None
        if path in _OPEN_PATHS:
            return await call_next(request)

        session = None
        ck = request.cookies.get(SESSION_COOKIE)
        if AUTH_ENABLED and ck:
            session = read_session(ck)
            if session:
                request.state.user = session[0]
        if request.state.user is None and API_TOKEN:
            authz = request.headers.get("authorization", "")
            if authz.lower().startswith("bearer ") and \
                    hmac.compare_digest(authz[7:].strip(), API_TOKEN):
                request.state.user = "ha"

        if path == "/display":
            supplied = request.headers.get(DISPLAY_HEADER)
            token_ok = bool(DISPLAY_TOKEN and supplied
                            and hmac.compare_digest(supplied, DISPLAY_TOKEN))
            open_display = not DISPLAY_TOKEN and not AUTH_ENABLED
            if not (token_ok or request.state.user or open_display):
                if supplied is not None or not AUTH_ENABLED:
                    return Response("not found", status_code=404)
                return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        elif AUTH_ENABLED and request.state.user is None:
            if request.method == "GET" and not path.startswith("/api/") \
                    and not path.endswith(".svg"):
                nxt = path + (("?" + request.url.query) if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        response = await call_next(request)
        # Sliding renewal: keep regularly-used devices signed in indefinitely.
        if session and int(time.time()) - session[1] > SESSION_RENEW_SECONDS:
            set_session_cookie(response, request, session[0])
        return response


app.add_middleware(AuthGate)


# ---------------------------------------------------------------------------
# Validation — the security boundary.
# ---------------------------------------------------------------------------

def validate_dog(value) -> str:
    if not isinstance(value, str) or value.lower() not in DOGS:
        raise ValueError(f"dog must be one of {DOGS}")
    return value.lower()


def validate_dog_or_all(value) -> str:
    """For standalone notes: a real dog OR the 'all' sentinel. Never used for
    bathroom events — only pee/poop/park rows must be a real dog."""
    if not isinstance(value, str) or value.lower() not in (DOGS + [DOG_SENTINEL]):
        raise ValueError(f"dog must be one of {DOGS + [DOG_SENTINEL]}")
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


def _http_date(epoch: int) -> str:
    """RFC 7231 IMF-fixdate in GMT, for the Last-Modified header."""
    from email.utils import formatdate
    return formatdate(epoch, usegmt=True)


def _parse_http_date(s: str):
    """Parse an HTTP-date (e.g. If-Modified-Since) to epoch, or None."""
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError):
        return None


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
        has_park = False
        for ev in slot["events"]:
            by_dog[ev["dog"]].append(ev)
            if ev["notes"]:
                has_note = True
            if ev["park"]:
                has_park = True
        out.append({
            "anchor_ts": slot["anchor_ts"],
            "has_note": has_note,
            "has_park": has_park,
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


def park_subline(conn, dog, kind, now_epoch):
    """For a pee/poop button: show '🌳 Xh ago' only if the dog's most recent
    park event is newer than its most recent true event of `kind`. Returns the
    park age string, or None to hide the line."""
    park = conn.execute(
        "SELECT timestamp FROM events WHERE dog = ? AND park IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1",
        (dog,),
    ).fetchone()
    if not park:
        return None
    true_ev = conn.execute(
        f"SELECT timestamp FROM events WHERE dog = ? AND {kind} IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1",
        (dog,),
    ).fetchone()
    # Hide the park line if a true event of this kind is newer than the park,
    # OR falls within the same session window as the park (covers out-of-order
    # logging on one walk: pee at 13:01, park tapped at 13:04 — the pee is the
    # real record for that session, park adds nothing).
    if true_ev:
        if true_ev["timestamp"] >= park["timestamp"]:
            return None
        if park["timestamp"] - true_ev["timestamp"] <= GROUP_WINDOW_SECONDS:
            return None
    return humanize_age(park["timestamp"], now_epoch)


def bingo_complete(conn, now_epoch):
    """True when every dog has a WITNESSED (location='outside') pee AND poop
    within the session window. Accidents ('inside') and park events don't
    count — a bingo is six real button taps."""
    cutoff = now_epoch - GROUP_WINDOW_SECONDS
    for d in DOGS:
        for kind in ("pee", "poop"):
            hit = conn.execute(
                f"SELECT 1 FROM events WHERE dog = ? AND {kind} = 'outside' "
                "AND timestamp >= ? LIMIT 1",
                (d, cutoff),
            ).fetchone()
            if not hit:
                return False
    return True


# Last time a bingo celebration fired (epoch). In-memory on purpose: a
# container restart forgetting it is harmless.
_last_bingo_epoch = 0


def button_states(now_epoch):
    """Per dog+kind button state for the whole /log page."""
    states = {d: {} for d in DOGS}
    with get_db() as conn:
        for d in DOGS:
            for kind in KINDS:
                st = compute_button_state(conn, d, kind, now_epoch)
                if kind in ("pee", "poop"):
                    st["park_age"] = park_subline(conn, d, kind, now_epoch)
                states[d][kind] = st
    return states


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    nxt = _safe_next(request.query_params.get("next"))
    if not AUTH_ENABLED:
        return RedirectResponse(nxt, status_code=303)
    ck = request.cookies.get(SESSION_COOKIE)
    if ck and read_session(ck):                       # already signed in
        return RedirectResponse(nxt, status_code=303)
    resp = templates.TemplateResponse(request, "login.html",
                                      {"next": nxt, "error": None, "username": ""})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/log", status_code=303)
    form = await request.form()
    user = str(form.get("username", "")).strip().lower()[:64]
    pw = str(form.get("password", ""))[:256]
    nxt = _safe_next(form.get("next"))
    stored = USERS.get(user)
    ok = check_password(pw, stored if stored else _DUMMY_HASH) and stored is not None
    if ok:
        resp = RedirectResponse(nxt, status_code=303)
        set_session_cookie(resp, request, user)
        return resp
    await asyncio.sleep(1)    # slows guessing; fail2ban handles the rest
    # 401 on a failed POST /login is the signal fail2ban keys on.
    resp = templates.TemplateResponse(
        request, "login.html",
        {"next": nxt, "error": "Wrong username or password.", "username": user},
        status_code=401)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/")
async def root():
    return RedirectResponse(url="/log", status_code=307)


@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request):
    now_epoch = int(time.time())
    states = button_states(now_epoch)
    now_local = datetime.fromtimestamp(now_epoch, LOCAL_TZ)
    resp = templates.TemplateResponse(
        request, "log.html",
        {"dogs": DOGS, "states": states,
         "now_str": now_local.strftime("%a %H:%M"),
         "window": GROUP_WINDOW_SECONDS},
    )
    # Don't let the browser keep/restore a stored copy of this page; any
    # restore must come back to the server. The page also refreshes its
    # state in place via /api/state (see log.html).
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/state")
async def api_state():
    """Current button state for every dog — exactly what /log renders.
    Read-only; used by /log to refresh itself in place on resume and on a
    timer, so a stale tab never shows (or acts on) old state."""
    now_epoch = int(time.time())
    now_local = datetime.fromtimestamp(now_epoch, LOCAL_TZ)
    return JSONResponse(
        {"ok": True, "now_str": now_local.strftime("%a %H:%M"),
         "states": button_states(now_epoch)},
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/display", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def display_page(request: Request):
    if DISPLAY_DEBUG:
        import sys
        _c = request.client.host if request.client else "?"
        print(f"[display] GET from {_c}", file=sys.stderr, flush=True)
        for _k, _v in request.headers.items():
            # Redact the token: confirm presence + last 4 chars, don't write the secret.
            if _k.lower() == DISPLAY_HEADER.lower():
                _v = f"<present, …{_v[-4:]}>" if _v else "<empty>"
            print(f"[display]   {_k}: {_v}", file=sys.stderr, flush=True)

    # Token / session check happens in AuthGate before we get here.
    now_epoch = int(time.time())
    cutoff = now_epoch - RETENTION_DAYS * 86400

    # --- Conditional-request support (ETag / Last-Modified) ---------------
    # Compute a validator that changes when (a) the displayed event data
    # changes, or (b) enough time passes that the relative "X ago" ages would
    # drift more than DISPLAY_MAX_STALE_SECONDS from truth. A browser never
    # sends If-None-Match, so it always gets a fresh live render; TRMNL sends
    # back our last validator and gets a 304 (skip re-render) when unchanged.
    with get_db() as conn:
        sig_rows = conn.execute(
            "SELECT id, timestamp, dog, pee, poop, park FROM events "
            "WHERE timestamp >= ? "
            "AND (pee IS NOT NULL OR poop IS NOT NULL OR park IS NOT NULL) "
            "ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
    stale_bucket = now_epoch // max(DISPLAY_MAX_STALE_SECONDS, 1)
    sig = "|".join(
        f"{r['id']}:{r['timestamp']}:{r['dog']}:{r['pee']}:{r['poop']}:{r['park']}"
        for r in sig_rows
    ) + f"|bucket={stale_bucket}"
    etag = '"' + hashlib.sha256(sig.encode()).hexdigest()[:32] + '"'
    # Last-Modified: the newest of (most recent event, start of current bucket),
    # so it advances with data changes and with each staleness bucket rollover.
    last_mod_epoch = max(
        (sig_rows[0]["timestamp"] if sig_rows else 0),
        stale_bucket * max(DISPLAY_MAX_STALE_SECONDS, 1),
    )
    last_mod_http = _http_date(last_mod_epoch)

    def _strip_weak(t):
        # Weak-comparison: a W/ prefix marks a weak validator; per RFC 7232
        # If-None-Match uses weak comparison, so strip it from both sides.
        # (TRMNL echoes our ETag back as W/"...", so an exact match would fail.)
        t = t.strip()
        if t.startswith("W/"):
            t = t[2:]
        return t

    inm = request.headers.get("if-none-match", "")
    if inm and _strip_weak(etag) in [_strip_weak(t) for t in inm.split(",")]:
        return Response(status_code=304, headers={
            "ETag": etag, "Last-Modified": last_mod_http, "Cache-Control": "no-cache",
        })
    # Fall back to If-Modified-Since when no matching ETag was sent.
    ims = request.headers.get("if-modified-since", "")
    if ims and not inm:
        ims_epoch = _parse_http_date(ims)
        if ims_epoch is not None and last_mod_epoch <= ims_epoch:
            return Response(status_code=304, headers={
                "ETag": etag, "Last-Modified": last_mod_http, "Cache-Control": "no-cache",
            })

    cond_headers = {"ETag": etag, "Last-Modified": last_mod_http, "Cache-Control": "no-cache"}

    with get_db() as conn:
        # Only bathroom events reach the wall display. Note-only rows (standalone
        # journal notes, incl. dog='all') carry no pee/poop/park and are excluded
        # so they neither consume a slot nor break the per-dog grouping.
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp >= ? "
            "AND (pee IS NOT NULL OR poop IS NOT NULL OR park IS NOT NULL) "
            "ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()

    now_local = datetime.fromtimestamp(now_epoch, LOCAL_TZ)
    slots = group_events(rows, now_epoch)[:DISPLAY_ROWS]
    ages = latest_per_dog(rows, now_epoch)
    for s in slots:
        s["time_str"] = format_row_time(s["anchor_ts"], now_local)

    resp = templates.TemplateResponse(
        request, "display.html",
        {"dogs": DOGS, "slots": slots, "ages": ages,
         "fetched_str": now_local.strftime("%a %H:%M")},
    )
    for _k, _v in cond_headers.items():
        resp.headers[_k] = _v
    return resp


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
        "id": r["id"],
        "dog": r["dog"],
        "dog_disp": ("All dogs" if r["dog"] == DOG_SENTINEL else r["dog"].capitalize()),
        "pee": r["pee"], "poop": r["poop"], "park": r["park"],
        "notes": r["notes"] or "",
        "note_only": not (r["pee"] or r["poop"] or r["park"]),
        "when": format_full_time(r["timestamp"]),
    } for r in rows]

    return templates.TemplateResponse(
        request, "advanced.html",
        {"dogs": DOGS, "events": events,
         "user": request.state.user, "auth": AUTH_ENABLED},
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
    stored = "yes" if kind == "park" else "outside"
    global _last_bingo_epoch
    with get_db() as conn:
        was_bingo = bingo_complete(conn, now_epoch)
        recent = recent_event_in_window(conn, dog, kind, now_epoch)
        if recent is not None:
            # Clear just this column; delete the row only if nothing else is on it.
            others = [k for k in EVENT_COLS if k != kind]
            remaining = any(recent[o] for o in others) or recent["notes"]
            if remaining:
                conn.execute(f"UPDATE events SET {kind} = NULL WHERE id = ?", (recent["id"],))
            else:
                conn.execute("DELETE FROM events WHERE id = ?", (recent["id"],))
            action = "removed"
        else:
            conn.execute(
                f"INSERT INTO events (timestamp, dog, {kind}, logged_by) VALUES (?, ?, ?, ?)",
                (now_epoch, dog, stored, request.state.user),
            )
            action = "logged"
        # Recompute the button's state AFTER the change so the client can render
        # it identically to a fresh page load (handles revert-to-historical and
        # the double-log-in-window edge for free).
        state = compute_button_state(conn, dog, kind, now_epoch)

        # Bingo fires only on the tap that TRANSITIONS the grid into complete
        # (page loads and already-complete states never fire), and only if the
        # cooldown has elapsed. Removals can't complete a grid, so gate on
        # 'logged' for clarity.
        bingo = False
        if action == "logged" and not was_bingo and bingo_complete(conn, now_epoch):
            if now_epoch - _last_bingo_epoch >= BINGO_COOLDOWN_SECONDS:
                bingo = True
                _last_bingo_epoch = now_epoch

    return {"ok": True, "action": action, "dog": dog, "kind": kind,
            "button": state, "bingo": bingo}


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

    # park is a flag, not a location: any truthy value stores the sentinel.
    park = "yes" if body.get("park") else None

    if pee is None and poop is None and park is None:
        return JSONResponse(
            {"error": "at least one of pee, poop, or park must be set"},
            status_code=400,
        )

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
            "INSERT INTO events (timestamp, dog, pee, poop, park, notes, logged_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, dog, pee, poop, park, notes, request.state.user),
        )
        new_id = cur.lastrowid
    return {"ok": True, "id": new_id, "timestamp": ts}


@app.post("/api/note")
async def api_note(request: Request):
    """Standalone journal note (food change, gland expression, vet visit, etc.).
    Body: {"dog": <real|all>, "notes": "...", "timestamp"?: epoch}. No pee/poop/
    park — this is a note-only row. dog may be the 'all' sentinel."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        dog = validate_dog_or_all(body.get("dog"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    notes = body.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        return JSONResponse({"error": "notes text is required"}, status_code=400)
    notes = notes[:500]

    now_epoch = int(time.time())
    try:
        ts = validate_timestamp(body.get("timestamp"), now_epoch)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO events (timestamp, dog, notes, logged_by) VALUES (?, ?, ?, ?)",
            (ts, dog, notes, request.state.user),
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


# ---------------------------------------------------------------------------
# History — browsable/filterable log, keyword note search, frequency graph
# ---------------------------------------------------------------------------

def _parse_date(s, default):
    """YYYY-MM-DD (local) -> epoch at local midnight. Blank/invalid -> default."""
    if not s:
        return default
    try:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
        return int(d.timestamp())
    except (ValueError, TypeError):
        return default


def query_history(dog=None, kind=None, notes_only=False, keyword=None,
                  start=None, end=None, limit=500):
    """Filtered event fetch for the history views.
      dog     : real dog, 'all', or None (any). 'all' matches only all-dog notes.
      kind    : 'pee'|'poop'|'park' or None. Filters to rows where that column is set.
      notes_only : only rows with a non-empty note.
      keyword : substring match within notes (case-insensitive).
      start/end : epoch bounds (inclusive start, exclusive end).
    """
    sql = ["SELECT * FROM events WHERE 1=1"]
    params = []
    if dog:
        sql.append("AND dog = ?"); params.append(dog)
    if kind in EVENT_COLS:
        sql.append(f"AND {kind} IS NOT NULL")   # kind validated against EVENT_COLS
    if notes_only:
        sql.append("AND notes IS NOT NULL AND notes != ''")
    if keyword:
        sql.append("AND notes LIKE ? ESCAPE '\\'")
        esc = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{esc}%")
    if start is not None:
        sql.append("AND timestamp >= ?"); params.append(start)
    if end is not None:
        sql.append("AND timestamp < ?"); params.append(end)
    sql.append("ORDER BY timestamp DESC LIMIT ?")
    params.append(limit)
    with get_db() as conn:
        return conn.execute(" ".join(sql), params).fetchall()


def _history_filters(request):
    """Pull + sanitize filter params shared by the page, API, and graph."""
    q = request.query_params
    dog = q.get("dog") or None
    if dog and dog not in (DOGS + [DOG_SENTINEL]):
        dog = None
    kind = q.get("kind") or None
    if kind not in EVENT_COLS:
        kind = None
    notes_only = q.get("notes_only") in ("1", "true", "on", "yes")
    keyword = (q.get("q") or "").strip() or None
    now_epoch = int(time.time())
    default_start = now_epoch - 30 * 86400
    start = _parse_date(q.get("start"), default_start)
    # Only bound the top end when the user explicitly picks an end date (then the
    # bound is that day's end, i.e. +1 day exclusive). With no end date, leave it
    # open so entries logged 'just now' aren't excluded by a now()-equal bound.
    end_raw = q.get("end")
    if end_raw:
        end = _parse_date(end_raw, now_epoch) + 86400
    else:
        end = None
    return {"dog": dog, "kind": kind, "notes_only": notes_only,
            "keyword": keyword, "start": start, "end": end}


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    f = _history_filters(request)
    rows = query_history(**f, limit=500)
    items = [{
        "id": r["id"],
        "dog": r["dog"],
        "dog_disp": ("All dogs" if r["dog"] == DOG_SENTINEL else r["dog"].capitalize()),
        "pee": r["pee"], "poop": r["poop"], "park": r["park"],
        "notes": r["notes"] or "",
        "note_only": not (r["pee"] or r["poop"] or r["park"]),
        "when": format_full_time(r["timestamp"]),
    } for r in rows]

    q = request.query_params
    # Prefill the date inputs with actual dates (one month ago -> today) when the
    # user hasn't set them, so the range is visible/editable rather than implicit.
    _now = datetime.now(LOCAL_TZ)
    default_start_str = (_now - timedelta(days=30)).strftime("%Y-%m-%d")
    default_end_str = _now.strftime("%Y-%m-%d")
    # graph shows only when a single dog + single type are chosen
    graph_ready = bool(f["dog"] and f["dog"] != DOG_SENTINEL and f["kind"])
    graph_qs = ""
    if graph_ready:
        from urllib.parse import urlencode
        gp = {"dog": f["dog"], "kind": f["kind"]}
        if q.get("start"): gp["start"] = q.get("start")
        if q.get("end"): gp["end"] = q.get("end")
        if q.get("q"): gp["q"] = q.get("q")
        graph_qs = urlencode(gp)

    return templates.TemplateResponse(
        request, "history.html",
        {
            "dogs": DOGS,
            "items": items,
            "count": len(items),
            "sel_dog": q.get("dog", ""),
            "sel_kind": q.get("kind", ""),
            "sel_notes_only": f["notes_only"],
            "sel_q": q.get("q", ""),
            "sel_start": q.get("start", ""),
            "sel_end": q.get("end", ""),
            "default_start": default_start_str,
            "default_end": default_end_str,
            "graph_ready": graph_ready,
            "graph_qs": graph_qs,
        },
    )


@app.get("/api/history")
async def api_history(request: Request):
    f = _history_filters(request)
    rows = query_history(**f, limit=1000)
    return {"ok": True, "count": len(rows), "events": [{
        "id": r["id"], "timestamp": r["timestamp"], "dog": r["dog"],
        "pee": r["pee"], "poop": r["poop"], "park": r["park"],
        "notes": r["notes"], "when": format_full_time(r["timestamp"]),
    } for r in rows]}


@app.get("/history/graph.svg")
async def history_graph(request: Request):
    """Server-rendered daily-frequency bar chart for one dog + one type.
    Monochrome, count labels above each bar. Requires dog (real) and kind."""
    f = _history_filters(request)
    if not (f["dog"] and f["dog"] != DOG_SENTINEL and f["kind"]):
        return Response("<svg xmlns='http://www.w3.org/2000/svg' width='700' height='60'>"
                        "<text x='12' y='36' font-family='Arial' font-size='16'>"
                        "Pick one dog and one type to see the graph.</text></svg>",
                        media_type="image/svg+xml")

    rows = query_history(dog=f["dog"], kind=f["kind"],
                         start=f["start"], end=f["end"], limit=100000)

    # bucket by local calendar day; also track which days have a keyword match.
    # When a keyword is present the graph HIGHLIGHTS rather than filters: every
    # bar for the range is drawn hollow, and any day with a matching note on an
    # event of this type is drawn solid. Bar height stays the total count.
    from collections import Counter
    counts = Counter()
    match_days = set()
    kw = (f["keyword"] or "").lower()
    for r in rows:
        day = datetime.fromtimestamp(r["timestamp"], LOCAL_TZ).date()
        counts[day] += 1
        if kw and r["notes"] and kw in r["notes"].lower():
            match_days.add(day)
    highlight = bool(kw)

    # build a continuous day axis from start..end so gaps show as zero
    end_epoch = f["end"] if f["end"] is not None else int(time.time()) + 1
    start_d = datetime.fromtimestamp(f["start"], LOCAL_TZ).date()
    end_d = datetime.fromtimestamp(end_epoch - 1, LOCAL_TZ).date()
    days = []
    d = start_d
    while d <= end_d:
        days.append(d)
        d += timedelta(days=1)
    if not days:
        days = [end_d]

    # cap the number of bars so a huge range stays legible; if too many days,
    # aggregate isn't done — instead we just widen and let it scroll-less fit.
    svg_w = 720
    pad_l, pad_r, pad_t, pad_b = 40, 16, 28, 46
    plot_w = svg_w - pad_l - pad_r
    plot_h = 220
    svg_h = plot_h + pad_t + pad_b
    n = len(days)
    bw = plot_w / max(n, 1)
    maxc = max([counts[d] for d in days] + [1])

    bars = []
    labels = []
    for i, day in enumerate(days):
        c = counts[day]
        x = pad_l + i * bw
        bh = (c / maxc) * plot_h if maxc else 0
        y = pad_t + (plot_h - bh)
        bar_w = max(bw * 0.7, 1)
        bx = x + (bw - bar_w) / 2
        if c > 0:
            # Default (no keyword): solid. Keyword mode: hollow unless this day
            # has a match, then solid.
            if not highlight or day in match_days:
                bar = (f"<rect x='{bx:.1f}' y='{y:.1f}' width='{bar_w:.1f}' "
                       f"height='{bh:.1f}' fill='#000'/>")
            else:
                bar = (f"<rect x='{bx:.1f}' y='{y:.1f}' width='{bar_w:.1f}' "
                       f"height='{bh:.1f}' fill='#fff' stroke='#000' "
                       f"stroke-width='1.5'/>")
            bars.append(bar)
            # count label above bar (only if bars aren't too dense)
            if n <= 45:
                labels.append(f"<text x='{x + bw/2:.1f}' y='{y - 4:.1f}' "
                              f"text-anchor='middle' font-family='Arial' "
                              f"font-size='11'>{c}</text>")
        # x-axis date label: show sparsely to avoid overlap
        step = max(1, n // 12)
        if i % step == 0:
            lbl = day.strftime("%m/%d")
            labels.append(f"<text x='{x + bw/2:.1f}' y='{pad_t + plot_h + 16:.1f}' "
                          f"text-anchor='middle' font-family='Arial' font-size='10' "
                          f"fill='#555'>{lbl}</text>")

    # y-axis: baseline + max tick
    axis = (f"<line x1='{pad_l}' y1='{pad_t + plot_h}' x2='{svg_w - pad_r}' "
            f"y2='{pad_t + plot_h}' stroke='#000' stroke-width='1'/>"
            f"<text x='{pad_l - 6}' y='{pad_t + 10}' text-anchor='end' "
            f"font-family='Arial' font-size='10' fill='#555'>{maxc}</text>"
            f"<text x='{pad_l - 6}' y='{pad_t + plot_h}' text-anchor='end' "
            f"font-family='Arial' font-size='10' fill='#555'>0</text>")

    title = f"{f['dog'].capitalize()} — {f['kind']} per day"
    if highlight:
        title += f"  (solid = notes matching \u201c{kw}\u201d)"
    header = (f"<text x='{pad_l}' y='18' font-family='Arial' font-size='14' "
              f"font-weight='bold'>{title}</text>")

    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='{svg_w}' height='{svg_h}' "
           f"viewBox='0 0 {svg_w} {svg_h}'>"
           f"<rect width='{svg_w}' height='{svg_h}' fill='#fff'/>"
           f"{header}{axis}{''.join(bars)}{''.join(labels)}</svg>")
    return Response(svg, media_type="image/svg+xml")


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


# --- Icons (auth-exempt, like /healthz) -----------------------------------
# Browsers probe these well-known paths; serve real assets so they don't 404.
# Paw-print icons live as static files (no runtime image generation).

def _icon(name, media):
    path = os.path.join(STATIC_DIR, name)
    if os.path.exists(path):
        return FileResponse(path, media_type=media)
    return Response(status_code=404)


@app.get("/favicon.ico")
async def favicon_ico():
    # Serve the SVG as the .ico target; modern browsers accept it.
    return _icon("favicon.svg", "image/svg+xml")


@app.get("/favicon.svg")
async def favicon_svg():
    return _icon("favicon.svg", "image/svg+xml")


@app.get("/favicon-32.png")
async def favicon_32():
    return _icon("favicon-32.png", "image/png")


@app.get("/favicon-16.png")
async def favicon_16():
    return _icon("favicon-16.png", "image/png")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
async def apple_touch_icon():
    return _icon("apple-touch-icon.png", "image/png")


@app.get("/apple-touch-icon-152.png")
async def apple_touch_icon_152():
    return _icon("apple-touch-icon-152.png", "image/png")


@app.get("/apple-touch-icon-120.png")
async def apple_touch_icon_120():
    return _icon("apple-touch-icon-120.png", "image/png")


if __name__ == "__main__":
    # Generate a USERS entry:  docker compose exec dog-tracker python main.py hashpw
    import getpass
    if len(sys.argv) > 1 and sys.argv[1] == "hashpw":
        name = input("Username: ").strip().lower()
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again:    "):
            sys.exit("Passwords didn't match.")
        print(f"\n{name}:{hash_password(pw)}")
        print("Add that to USERS in .env (comma-separate multiple users), then:")
        print("  docker compose up -d dog-tracker")
    else:
        print("usage: python main.py hashpw")
