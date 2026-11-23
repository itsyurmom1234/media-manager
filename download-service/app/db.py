import os
import uuid
import datetime as dt

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://media:media@postgres:5432/media"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=False)
    url = Column(String, nullable=False)
    total_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    url = Column(String, nullable=False)
    title = Column(String, nullable=True)
    status = Column(String, default="queued")  # queued -> running -> done | failed
    file_path = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    playlist_id = Column(String, nullable=True)
    playlist_index = Column(Integer, nullable=True)  # 1-based position in the playlist
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    _run_light_migrations()


def _run_light_migrations():
    """Adds columns introduced after the table already existed, so an
    existing running stack doesn't need its database wiped. Safe to
    run every startup - each statement is a no-op if already applied."""
    statements = [
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS title VARCHAR",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS playlist_id VARCHAR",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS playlist_index INTEGER",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def get_session():
    return SessionLocal()
