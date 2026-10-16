import asyncio
import contextlib
import select
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Literal

import psycopg2
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session


# Fail-fast configuration: The app refuses to start if DATABASE_URL is missing.
# Great for demonstrating missing environment variables on the VM or in systemd.
class Settings(BaseSettings):
    database_url: str
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_is_postgres = settings.database_url.startswith("postgresql")
SSE_CHANNEL = "maoni_notes"

# --- Server-Sent Events plumbing ---
# _sse_clients holds one asyncio.Queue per connected browser. Every watcher
# gets a `notes_changed` event the moment any note is created or liked.
_sse_clients: set[asyncio.Queue] = set()
_worker_loop: asyncio.AbstractEventLoop | None = None


def _push_to_sse(message: str) -> None:
    """Safe to call from any thread (sync endpoints run in a threadpool)."""
    if _worker_loop is None or not _sse_clients:
        return
    for queue in list(_sse_clients):
        _worker_loop.call_soon_threadsafe(queue.put_nowait, message)


def notify_notes_changed() -> None:
    """Wake every connected client immediately.

    With Postgres we go through NOTIFY so this also works if you scale to
    several app processes/workers. Without Postgres we push directly to the
    clients attached to this process.
    """
    if _is_postgres:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(f"SELECT pg_notify('{SSE_CHANNEL}', 'refresh')")
                )
        except Exception:
            # Never let notification failures break the user's request.
            _push_to_sse("refresh")
    else:
        _push_to_sse("refresh")


def _listen_for_changes() -> None:
    """Background thread holding one dedicated LISTEN connection.

    PostgreSQL pushes into this connection the instant any process notifies,
    and we fan out to all attached SSE clients.
    """
    while True:
        conn = None
        try:
            conn = psycopg2.connect(settings.database_url)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {SSE_CHANNEL}")
            while True:
                if select.select([conn], [], [], 10) == ([], [], []):
                    continue
                conn.poll()
                if conn.notifies:
                    conn.notifies.clear()
                    _push_to_sse("refresh")
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            time.sleep(3)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_loop
    _worker_loop = asyncio.get_running_loop()
    if _is_postgres:
        threading.Thread(target=_listen_for_changes, daemon=True).start()
    yield


# --- Database Model ---
class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    recipient = Column(String(50), nullable=False)
    message = Column(String(280), nullable=False)
    sender = Column(String(50), default="Anonymous")
    likes = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# Auto-create tables on startup so you don't need Alembic migrations during the demo
Base.metadata.create_all(bind=engine)


# --- Pydantic Schemas ---
class NoteCreate(BaseModel):
    recipient: str = Field(..., min_length=1, max_length=50)
    message: str = Field(..., min_length=1, max_length=280)
    sender: str = Field(default="Anonymous", max_length=50)


class NoteResponse(BaseModel):
    id: int
    recipient: str
    message: str
    sender: str
    likes: int
    created_at: datetime

    class Config:
        from_attributes = True


# --- FastAPI App & Dependencies ---
app = FastAPI(title="Maoni Feedback Wall", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Routes ---
@app.get("/api/meta")
def get_meta():
    """Returns the hostname of the machine/container running the app."""
    return {"hostname": socket.gethostname()}


@app.get("/api/notes", response_model=list[NoteResponse])
def get_notes(sort: Literal["newest", "popular"] = "newest", db: Session = Depends(get_db)):
    query = db.query(Note)
    if sort == "popular":
        query = query.order_by(Note.likes.desc(), Note.created_at.desc())
    else:
        query = query.order_by(Note.created_at.desc())
    return query.limit(100).all()


@app.get("/api/notes/stream")
async def notes_stream(request: Request):
    """Server-Sent Events feed: emits `notes_changed` the instant the wall changes.

    The frontend refetches on this signal, so new notes and like counts appear
    in every open browser within milliseconds.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _sse_clients.add(queue)
    try:
        yield ": connected\n\n"
        while not await request.is_disconnected():
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"event: notes_changed\ndata: {message}\n\n"
    finally:
        _sse_clients.discard(queue)


@app.post("/api/notes", response_model=NoteResponse, status_code=201)
def create_note(payload: NoteCreate, db: Session = Depends(get_db)):
    clean_sender = payload.sender.strip() or "Anonymous"
    note = Note(
        recipient=payload.recipient.strip(),
        message=payload.message.strip(),
        sender=clean_sender,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    notify_notes_changed()
    return note


@app.post("/api/notes/{note_id}/like", response_model=NoteResponse)
def like_note(note_id: int, db: Session = Depends(get_db)):
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    note.likes += 1
    db.commit()
    db.refresh(note)
    notify_notes_changed()
    return note


@app.get("/")
def serve_ui():
    return FileResponse("static/index.html")
