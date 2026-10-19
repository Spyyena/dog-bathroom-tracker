# Dog bathroom tracker

A small self-hosted web app for logging when the dogs pee, poop, and visit the
park, with a wall-mounted e-ink display, a journal/notes system, and a
browsable history with frequency graphs. Replaces the whiteboard that someone
always forgets to update.

- **Phone page** for one-tap logging (tap to log, tap again to undo).
- **Wall display** optimised for an 800×480 e-ink panel (built for TRMNL, but
  it's just an HTML page any screen can show).
- **Advanced page** for corrections, accidents, notes, and backdated entries.
- **History page** with filtering, keyword note search, and per-day frequency
  graphs.
- **Built-in login** with long-lived sessions, plus a bearer token for
  automations.

Backend is FastAPI, data is a single SQLite file. Designed to run as a Docker
container behind an nginx reverse proxy — the author runs it in a Proxmox LXC,
but it'll run anywhere Docker does. The app is host- and domain-agnostic and
uses relative paths throughout.

> **Up to three dogs.** The display layout is built around three. Dog names live
> in one list at the top of `main.py` (`DOGS = [...]`) — generic sample names are
> included; edit them to your own. Adding a fourth dog needs display rework, not
> just a list change.

## Features

**Logging (`/log`)**
- A pee and poop button per dog. Tap to log, tap the same button again to undo.
- A small park (🌳) button per dog for off-leash / unsupervised visits.
- Undo is window-bound: a second tap removes a just-logged event as long as it's
  within the grouping window (default 20 min). Server-determined, so it survives
  a page refresh.
- Each button shows how long since that dog last did that thing.
- The page polls `/api/state` so a second phone sees the other's taps without a
  refresh.

**Wall display (`/display`)**
- A clean table of recent walks plus a per-dog status strip.
- Same-walk grouping: events logged close together collapse into one row.
- Markers appear in the time column when a row has a note (📝) or a park visit
  (🌳). Note-only journal rows never appear here.
- Gated by a secret header so only the display device can fetch it.
- Supports conditional requests (ETag / Last-Modified → 304) so the device only
  re-renders when something changed, with a forced refresh after
  `DISPLAY_MAX_STALE_SECONDS` so the "time since" figures don't go stale.

**Accidents & notes (`/advanced`)**
- Log an indoor pee/poop (shown with a house marker 🏠 in that dog's column).
- Delete or edit any recent entry (the mis-tap fix).
- Add a note to any entry.
- **Standalone notes**: a journal entry not tied to any pee/poop — food changes,
  gland expression, vet visits. Assign to one dog or to "Household (all dogs)".
- Backdated entries: pick a dog, what, and a date + time.
- Shows who is signed in, with a **Sign out** button.

**History (`/history`)**
- Filter by dog, type, date range, notes-only, and keyword search within notes.
  (Keyword is a substring match, so "dia" catches every misspelling of diarrhea.)
- Date range prefills to the last 30 days.
- With one dog + one type selected, a server-rendered per-day frequency bar
  graph appears. Add a keyword and matching days are drawn solid against the rest
  hollow — e.g. one dog's poop trend with the "diarrhea" days highlighted.

**Bingo 🎉**
- When all three dogs have logged a witnessed (outdoor) pee *and* poop within the
  grouping window, the completing tap triggers a random celebration animation on
  the log page: rain, confetti, fireworks, rainbow, solitaire cascade, slot
  machine, zoomies, chase, stamp spree, balloons, or bouncing balls. Fires once
  per transition, rate-limited by `BINGO_COOLDOWN_SECONDS`. Accidents and park
  visits don't count toward bingo.

**Housekeeping**
- Rolling retention (default 30 days); a prune endpoint handles cleanup, meant
  for a cron job.
- Self-migrating schema: new columns are added to an existing database
  automatically on startup, so upgrades preserve history.
- Every logged event records who logged it (`logged_by`).

## File layout

```
dog-tracker/
├── Dockerfile
├── main.py
├── requirements.txt
├── static/               # favicons / apple-touch icons
└── templates/
    ├── login.html        # sign-in page
    ├── display.html      # wall display
    ├── log.html          # phone logging page (+ bingo animations)
    ├── advanced.html     # corrections, accidents, notes, backdated entries
    └── history.html      # filtering, search, frequency graph
```

Python dependencies (pinned in `requirements.txt`): `fastapi`, `uvicorn[standard]`,
`jinja2`, `python-multipart` (needed for the login form).

## Quick start

1. Drop the `dog-tracker/` folder on the Docker host.
2. Add the service to a `compose.yaml` (block below).
3. Create a `.env` next to the compose file with the secrets:
   ```bash
   echo "DISPLAY_TOKEN=$(openssl rand -hex 24)"  >> .env
   echo "SESSION_SECRET=$(openssl rand -hex 32)" >> .env
   echo "API_TOKEN=$(openssl rand -hex 24)"      >> .env
   ```
4. Build and start:
   ```bash
   docker compose up -d --build dog-tracker
   docker compose logs -f dog-tracker      # wait for "Application startup complete"
   ```
5. Create logins (see [Authentication](#authentication)), add them to `.env` as
   `USERS=`, and run `docker compose up -d dog-tracker` to apply.

With `USERS` empty the app runs **open** (no login) — handy for first-run
testing, not for anything internet-facing.

> **Templates must sit in `dog-tracker/templates/`** and icons in
> `dog-tracker/static/`. The Dockerfile copies them in at build time. If you copy
> files over a network and they land loose in `dog-tracker/`, move them before
> building.

### Compose service block

```yaml
services:
  dog-tracker:
    container_name: dog-tracker
    build: ./dog-tracker
    ports:
      - "5007:5007"
    environment:
      - TZ=America/Denver
      - DISPLAY_TOKEN=${DISPLAY_TOKEN:-}
      - DISPLAY_HEADER=X-Display-Token
      - RETENTION_DAYS=30
      - GROUP_WINDOW_SECONDS=1200
      - DISPLAY_ROWS=5
      - BINGO_COOLDOWN_SECONDS=3600
      # Auth: secret values live in .env
      - USERS=${USERS:-}
      - SESSION_SECRET=${SESSION_SECRET:-}
      - API_TOKEN=${API_TOKEN:-}
      - SESSION_DAYS=90
    volumes:
      - ./dog-tracker/data:/app/data    # SQLite lives here, on the host
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5007/healthz').status==200 else 1)\""]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 15s
    restart: unless-stopped
```

Compose only reads `.env` for `${...}` substitution — a variable in `.env` does
nothing unless the `environment:` block passes it through like the lines above.
In `.env`, put comments on their own lines rather than after a value.

Because it builds from local source, an image-pull auto-update job skips it.
To update after changing code: `docker compose up -d --build dog-tracker`.
Env-only changes need just `docker compose up -d dog-tracker`.

## Configuration (environment variables)

Values set in the compose `environment:` block override the code defaults.
When changing a runtime value, check the compose block first — it may be
overriding `main.py`.

| Variable | Default | Purpose |
|---|---|---|
| `TZ` | `America/Denver` | Timezone for display + age calculations |
| `USERS` | *(empty)* | Logins, `name:hash,name:hash` (see below). Empty = auth off |
| `SESSION_SECRET` | *(random per start)* | Signs session cookies. Unset → everyone is signed out on every restart |
| `SESSION_DAYS` | `365` | Session lifetime; renewed on use, so it only lapses after this long *idle* |
| `SESSION_RENEW_SECONDS` | `86400` | How old a session cookie gets before it's reissued (sliding renewal) |
| `API_TOKEN` | *(empty)* | Bearer token for automations; empty disables bearer access |
| `DISPLAY_TOKEN` | *(empty)* | Secret for the display gate; empty disables it |
| `DISPLAY_HEADER` | `X-Display-Token` | Header name the display gate checks |
| `DISPLAY_ROWS` | `5` | Max grouped rows on the wall display |
| `DISPLAY_MAX_STALE_SECONDS` | `7200` | Force a fresh display render after this long even if nothing changed |
| `DISPLAY_DEBUG` | `false` | Log incoming display request headers (token redacted) |
| `RETENTION_DAYS` | `30` | Days of history to keep |
| `GROUP_WINDOW_SECONDS` | `1200` | Same-walk grouping / undo window (20 min) |
| `ADVANCED_ROWS` | `30` | Max entries listed on the Advanced page |
| `BINGO_COOLDOWN_SECONDS` | `3600` | Min seconds between bingo celebrations; `0` disables cooldown |
| `DB_PATH` | `/app/data/log.db` | SQLite location (inside container) |
| `TEMPLATE_DIR` | `/app/templates` | Template location (inside container) |
| `STATIC_DIR` | `/app/static` | Icon location (inside container) |

## Authentication

Auth is handled by the app itself, not the proxy. **Every route requires a
signed-in session or the API token, except** `/login`, `/logout`, `/healthz`,
the favicons, and `/display` (which has its own token). New routes are protected
automatically — the check is a middleware in front of everything, so nothing
has to be added to an allow-list.

### Creating a login

```bash
docker compose exec dog-tracker python main.py hashpw
```

It prompts for a username and password (twice) and prints a line like
`alice:scrypt.16384.8.1.<salt>.<hash>`. Join entries with commas in `.env`:

```
USERS=alice:scrypt.16384...,bob:scrypt.16384...
```

Then `docker compose up -d dog-tracker`. Usernames are case-insensitive;
passwords are not. Only the scrypt hash is stored — never the password.

### Sessions

- Signing in sets an HttpOnly, SameSite=Lax cookie (`Secure` when the proxy
  reports HTTPS via `X-Forwarded-Proto`).
- Sessions **slide**: a cookie older than `SESSION_RENEW_SECONDS` is reissued on
  the next request, so a phone used regularly stays signed in indefinitely.
  Only `SESSION_DAYS` of *inactivity* ends a session.
- An open tab whose session lapses is bounced to the login page on its next
  action, then returned to where it was.
- Failed sign-ins wait one second and return **401** — that's the line a
  log-watching tool like fail2ban can key on.

### Revoking access

| To sign out… | Do this |
|---|---|
| One user, everywhere | Change their password (new `hashpw`, replace their entry) |
| One user, permanently | Remove their entry from `USERS` |
| Everyone | Rotate `SESSION_SECRET` |
| Just this browser | **Sign out** on the Advanced page |

All of these take effect on `docker compose up -d dog-tracker`.

### API token (automations)

Scripts and home-automation tools send:

```
Authorization: Bearer <API_TOKEN>
```

The token has full access (same as a signed-in user). Events it logs are
recorded with `logged_by = 'ha'`. Example:

```bash
curl -X POST http://<host>:5007/api/toggle \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"dog":"rex","kind":"pee"}'
```

Automations on the same network can call the container directly on port 5007
instead of going through the proxy.

## Reverse proxy

Point a proxy host at `http://<host>:5007` with your TLS cert. No custom
locations or proxy-level auth are needed — the app does its own. nginx forwards
the display's custom header and `X-Forwarded-Proto` by default.

If you previously used proxy basic auth in front of this app, remove it: it
double-prompts and defeats the long-lived sessions.

## Display device (e-ink / TRMNL etc.)

Point a screenshot/URL plugin at `https://<host>/display` and add the custom
header so it passes the gate:

```
X-Display-Token: <the value from your .env>
```

A wrong or missing token returns 404 (the page "doesn't exist"). A signed-in
browser can also open `/display` for previewing. Greyscale / 1-bit rendering is
fine — the emoji read clearly as silhouettes. Any refresh interval works.

## Retention prune (cron)

With auth on, the prune call needs the API token. Silent on success, mail only
on failure:

```cron
30 4 * * * curl -fsS -X POST -H "Authorization: Bearer <API_TOKEN>" http://localhost:5007/api/prune > /tmp/dog-prune.log 2>&1 || (cat /tmp/dog-prune.log; echo "dog tracker prune failed")
```

## Routes

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | ✅ | Redirects to `/log` |
| `/login` | GET, POST | open | Sign-in page / form submit |
| `/logout` | GET, POST | open | Clear session, back to `/login` |
| `/log` | GET | ✅ | Phone logging page |
| `/display` | GET, HEAD | token | Wall display (`X-Display-Token`, or a session) |
| `/advanced` | GET | ✅ | Corrections, accidents, notes, backdating |
| `/history` | GET | ✅ | Filter/search history + graph |
| `/history/graph.svg` | GET | ✅ | Server-rendered frequency graph |
| `/api/state` | GET | ✅ | Button states (log page polling) |
| `/api/toggle` | POST | ✅ | Log/un-log a pee/poop/park (main buttons) |
| `/api/log` | POST | ✅ | Insert an event; optional `timestamp` to backdate |
| `/api/note` | POST | ✅ | Standalone journal note (dog or `all`) |
| `/api/events` | GET | ✅ | Recent events (Advanced list) |
| `/api/history` | GET | ✅ | Filtered history as JSON |
| `/api/event/{id}` | DELETE | ✅ | Delete one event |
| `/api/event/{id}` | PATCH | ✅ | Edit one event |
| `/api/prune` | POST | ✅ | Delete events past retention |
| `/healthz` | GET | open | Health check (Docker) |
| `/favicon.*`, `/apple-touch-icon*.png` | GET | open | Icons |

✅ = session cookie or `Authorization: Bearer <API_TOKEN>`. Unauthenticated
page loads redirect to `/login`; unauthenticated API calls get `401` JSON.

## Data

```
id        INTEGER PK
timestamp INTEGER   unix epoch, UTC
dog       TEXT      a configured dog name, or 'all' for a household note
pee       TEXT      NULL | 'outside' | 'inside'
poop      TEXT      NULL | 'outside' | 'inside'
park      TEXT      NULL | 'yes'
notes     TEXT      nullable
logged_by TEXT      username, 'ha' for the API token, NULL for older rows / auth off
```

- `inside` renders as 🏠. `park='yes'` is a flag, not a location.
- A **note-only row** has pee/poop/park all NULL — that's a standalone journal
  entry. `dog='all'` marks a household note.
- One row per logged press; grouping into display rows happens at query time, so
  the window can be re-tuned without touching stored data.

Inspect or hand-edit anytime — it's just SQLite:

```bash
sqlite3 dog-tracker/data/log.db \
  "SELECT id, datetime(timestamp,'unixepoch','localtime'), dog, pee, poop, park, logged_by, notes
   FROM events ORDER BY timestamp DESC LIMIT 20;"
```

## Notes

- Backdated entries use separate native date + time inputs (nicer wheel than a
  combined control on both iOS and Android).
- Physical buttons could be added later (e.g. an ESP32 wired to buttons): they'd
  POST the same JSON to `/api/toggle` or `/api/log` with the bearer token. No app
  change needed.
- `.env` and `data/` should not be committed — see `.gitignore`.
