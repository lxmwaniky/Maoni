import asyncio
import contextlib
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Literal

import psycopg
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session


# Fail-fast configuration: The app refuses to start if DATABASE_URL is missing.
class Settings(BaseSettings):
    database_url: str
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_is_postgres = settings.database_url.startswith("postgresql")
SSE_CHANNEL = "maoni_notes"

# --- Server-Sent Events (SSE) Plumbing ---
# Holds one asyncio.Queue per connected browser tab.
_sse_clients: set[asyncio.Queue] = set()
_worker_loop: asyncio.AbstractEventLoop | None = None


def _push_to_sse(message: str = "refresh") -> None:
    """Thread-safe fan-out: wakes up every connected browser's SSE stream."""
    if _worker_loop is None or not _sse_clients:
        return
    for queue in list(_sse_clients):
        _worker_loop.call_soon_threadsafe(queue.put_nowait, message)


def _listen_for_changes() -> None:
    """Background thread holding a dedicated Postgres LISTEN connection."""
    # Convert SQLAlchemy URL (e.g. postgresql+psycopg://) to standard libpq URI
    pg_uri = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    while True:
        try:
            with psycopg.connect(pg_uri, autocommit=True) as conn:
                conn.execute(f"LISTEN {SSE_CHANNEL}")
                for notify in conn.notifies():
                    _push_to_sse(notify.payload or "refresh")
        except Exception:
            time.sleep(3)


def _install_pg_trigger() -> None:
    """Installs a Postgres trigger so even raw SQL changes in psql fire SSE!"""
    if not _is_postgres:
        return
    ddl = f"""
    CREATE OR REPLACE FUNCTION notify_maoni_changes() RETURNS trigger AS $$
    BEGIN
        PERFORM pg_notify('{SSE_CHANNEL}', 'refresh');
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS maoni_notes_trigger ON notes;
    CREATE TRIGGER maoni_notes_trigger
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON notes
    FOR EACH STATEMENT EXECUTE FUNCTION notify_maoni_changes();
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_loop
    _worker_loop = asyncio.get_running_loop()
    if _is_postgres:
        _install_pg_trigger()
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
    """Server-Sent Events (SSE) stream that pushes instant updates to browsers."""
    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        _sse_clients.add(queue)
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: notes_changed\ndata: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_clients.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Tells Nginx NOT to buffer SSE events!
        },
    )


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
    if not _is_postgres:
        _push_to_sse()
    return note


@app.post("/api/notes/{note_id}/like", response_model=NoteResponse)
def like_note(note_id: int, db: Session = Depends(get_db)):
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    note.likes += 1
    db.commit()
    db.refresh(note)
    if not _is_postgres:
        _push_to_sse()
    return note


@app.get("/")
def serve_ui():
    return FileResponse("static/index.html")