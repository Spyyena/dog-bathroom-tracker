# Dog bathroom tracker

A small self-hosted web app for logging when your dogs pee and poop, with a
wall-mounted e-ink display (built for [TRMNL](https://trmnl.com/), but the
display is just an HTML page any screen can show). Replaces the whiteboard
that someone always forgets to update.

Phone page for one-tap logging, a glanceable display for the wall, and an
Advanced page for corrections, accidents, notes, and backdated entries.
Data is a single SQLite file. Backend is FastAPI.

It's designed to run as a Docker container behind an nginx reverse proxy
(the author runs it in a Proxmox LXC, but it'll run anywhere Docker does —
the app is host- and domain-agnostic and uses relative paths throughout).

## Features

- **One-tap logging** (`/log`): a button per dog for pee and poop. Tap to log,
  tap again to un-log a mistake. Each button shows how long since that dog last
  did that thing.
- **Undo within a window**: a second tap removes a just-logged event, as long as
  it's within the grouping window (default 5 min). Survives a page refresh — the
  app determines undo-eligibility server-side, not from page state.
- **Wall display** (`/display`): a clean table of recent walks plus a per-dog
  status strip ("pee 2h, poop 9h"). Sized for an 800x480 panel. Optionally gated
  by a secret header so only your display device can fetch it.
- **Same-walk grouping**: events logged close together (within the window)
  collapse into one row, so two dogs done on the same walk share a line. Grouping
  happens at read time; raw per-event data is always preserved.
- **Accidents**: log an indoor pee/poop, shown with a house marker in that dog's
  column.
- **Notes**: attach a note to any entry (vomit, diarrhea, anything unusual). A
  small marker appears on the display for rows that have a note, so you know to
  check the app — without putting the note text on the wall.
- **Backdated entries**: "we forgot to log it an hour ago" — pick a dog, what,
  and a date + time, on the Advanced page.
- **Delete / edit**: fix mistakes on the Advanced page.
- **Rolling retention**: keeps a configurable number of days (default 7); a prune
  endpoint handles cleanup, meant to be hit by a cron job.

> **Up to three dogs.** The display layout is built around three. Dog names live
> in one list at the top of `main.py` (`DOGS = [...]`) — generic sample names are
> included; edit them to your own. Adding a fourth dog would need display rework,
> not just a list change.

## File layout

```
dog-tracker/
├── Dockerfile
├── main.py
├── requirements.txt
└── templates/
    ├── display.html      # the wall display
    ├── log.html          # phone logging page
    └── advanced.html     # corrections, accidents, notes, backdated entries
```

Bring it up alongside other services in a shared compose file, or on its own.

## Quick start

1. Drop the `dog-tracker/` folder somewhere on your Docker host.
2. Add the service to a `compose.yaml` (see the service block below).
3. Create a `.env` next to the compose file with a display token:
   ```bash
   echo "DISPLAY_TOKEN=$(openssl rand -hex 24)" >> .env
   ```
   Leave it empty to disable the display gate while testing.
4. Build and start:
   ```bash
   docker compose up -d --build dog-tracker
   docker compose logs -f dog-tracker      # wait for "Application startup complete"
   ```

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
      - RETENTION_DAYS=7
      - GROUP_WINDOW_SECONDS=300
      - DISPLAY_ROWS=5
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

Because it builds from local source, an image-pull auto-update job will skip it.
To update after changing the code: `docker compose up -d --build dog-tracker`.

> **Templates must sit in `dog-tracker/templates/`.** The Dockerfile copies them
> in at build time. If you copy files over a network and they land loose in
> `dog-tracker/`, move them into `templates/` before building.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `TZ` | `America/Denver` | Timezone for display + age calculations |
| `DISPLAY_TOKEN` | *(empty)* | Secret for the display gate; empty disables it |
| `DISPLAY_HEADER` | `X-Display-Token` | Header name the display gate checks |
| `RETENTION_DAYS` | `7` | How many days of history to keep |
| `GROUP_WINDOW_SECONDS` | `300` | Same-walk grouping / undo window |
| `DISPLAY_ROWS` | `5` | Max grouped rows on the wall display |
| `ADVANCED_ROWS` | `30` | Max entries listed on the Advanced page |
| `DB_PATH` | `/app/data/log.db` | SQLite location (inside container) |
| `TEMPLATE_DIR` | `/app/templates` | Template location (inside container) |

## Reverse proxy

Point a proxy host at `http://<host>:5007` with your TLS cert. No special config
is needed to forward the display's custom header — nginx passes request headers
through by default.

If you want to keep the logging side from being world-writable, put basic auth on
`/log` and `/api/log` while leaving `/display` reachable (it's gated by its own
header token instead). The data isn't sensitive, but an open write endpoint
invites automated noise.

## Display device (e-ink / TRMNL etc.)

Point a screenshot/URL plugin at `https://<your-host>/display` and add the custom
header so it passes the gate:

```
X-Display-Token: <the value from your .env>
```

Greyscale / 1-bit rendering is fine — the emoji read clearly as silhouettes. Any
refresh interval works; even hourly is plenty for "have they been out recently."

## Retention prune (cron)

Silent on success, mail only on failure:

```cron
30 4 * * * curl -fsS -X POST http://localhost:5007/api/prune > /tmp/dog-prune.log 2>&1 || (cat /tmp/dog-prune.log; echo "dog tracker prune failed")
```

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Redirects to `/log` |
| `/log` | GET | Phone logging page |
| `/display` | GET | Wall display (header-gated if token set) |
| `/advanced` | GET | Corrections, accidents, notes, backdated entries |
| `/api/toggle` | POST | Log or un-log a pee/poop (main-page buttons) |
| `/api/log` | POST | Insert an event; optional `timestamp` for backdating |
| `/api/events` | GET | Recent events (Advanced list) |
| `/api/event/{id}` | DELETE | Delete one event |
| `/api/event/{id}` | PATCH | Edit one event (location / notes) |
| `/api/prune` | POST | Delete events older than retention |
| `/healthz` | GET | Health check |

## Data

```
id        INTEGER PK
timestamp INTEGER   unix epoch, UTC
dog       TEXT      one of the configured dog names
pee       TEXT      NULL | 'outside' | 'inside'
poop      TEXT      NULL | 'outside' | 'inside'
notes     TEXT      nullable
```

`inside` renders as a house marker. One row per logged press; grouping into
display rows happens at query time, so the window can be re-tuned without
touching stored data.

Inspect or hand-edit anytime — it's just SQLite:

```bash
sqlite3 dog-tracker/data/log.db \
  "SELECT id, datetime(timestamp,'unixepoch','localtime'), dog, pee, poop, notes
   FROM events ORDER BY timestamp DESC LIMIT 20;"
```

## Notes

- The phone date/time picker for backdated entries uses separate native date and
  time inputs, which give a nicer wheel than a combined control on both iOS and
  Android.
- Physical buttons could be added later (e.g. an ESP32 wired to buttons): they'd
  just POST the same JSON to `/api/log`. No app change required.
- `.env` and `data/` should not be committed — add them to `.gitignore`.
