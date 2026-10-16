# Maoni — Session Notes Wall

Maoni is a live feedback wall: session attendees write short anonymous (or signed) notes — thoughts, questions, warm fuzzies — and stick them up on a shared board everyone can see, updated in real time.

## Stack

| Layer | Tech |
| --- | --- |
| Backend | Python + FastAPI |
| Database | PostgreSQL 16 (SQLAlchemy ORM) |
| Realtime | Server-Sent Events + Postgres `LISTEN/NOTIFY` |
| Frontend | Server-rendered HTML, Alpine.js, Tailwind |
| Tooling | `uv` for dependency management |
| Deployment | Docker Compose (app + Postgres), GCP-ready |

## How realtime works

When a note is created or liked, the backend publishes a `notes_changed` event:

1. On Postgres, the write path issues `pg_notify`, so the event reaches **every** app process, even behind multiple workers or replicas.
2. A dedicated background connection runs `LISTEN maoni_notes` and fans the signal out over **Server-Sent Events** (`GET /api/notes/stream`) to all connected browsers.
3. Each browser refetches the feed the moment the signal lands — new notes and like counts appear in every open tab in milliseconds.

If the SSE connection drops (network hiccup, proxy timeout), the browser automatically reconnects and falls back to quiet 5-second polling, so the wall is never stale, it just degrades gracefully. No WebSocket infrastructure, no extra services.

## Quick start (Docker)

```bash
docker compose up --build
```

Then open http://localhost:8000. The app talks to the `db` service automatically; the database URL comes from `DATABASE_URL` in `docker-compose.yml`.

## Local development

Requires Python 3.11+ and a running Postgres. Set the connection string in `.env` (copy from `.env.example`):

```
DATABASE_URL=postgresql://user:password@localhost:5432/maoni
```

Then:

```bash
uv sync
uv run uvicorn main:app --reload --port 8000
```

Open http://localhost:8000. The app refuses to start without `DATABASE_URL` — that's deliberate, to demonstrate fail-fast config on a VM or in systemd.

Tables are created automatically on startup, so no migration step is needed for the demo.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/notes?sort=newest/popular` | List notes (max 100) |
| `POST` | `/api/notes` | Create a note `{recipient, message, sender?}` |
| `POST` | `/api/notes/{id}/like` | Like a note |
| `GET` | `/api/notes/stream` | SSE feed of `notes_changed` events |
| `GET` | `/api/meta` | Hostname of the instance serving you (used in the footer) |
| `GET` | `/` | Single-page UI |


## License

MIT
