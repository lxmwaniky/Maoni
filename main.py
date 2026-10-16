import socket
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, Column, Integer, String, DateTime
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
app = FastAPI(title="Maoni Feedback Wall")
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
    return note


@app.post("/api/notes/{note_id}/like", response_model=NoteResponse)
def like_note(note_id: int, db: Session = Depends(get_db)):
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    note.likes += 1
    db.commit()
    db.refresh(note)
    return note


@app.get("/")
def serve_ui():
    return FileResponse("static/index.html")