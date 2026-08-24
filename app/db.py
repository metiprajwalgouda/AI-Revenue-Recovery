"""
SQLite database setup. Using SQLite (not Postgres/MySQL) deliberately -- it's a
single file, needs zero setup/server, and is genuinely fine for a buildathon demo's
data volume. Swapping to Postgres later would only mean changing DATABASE_URL.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db_models import Base

DATABASE_URL = "sqlite:///./data/storefront.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Creates all tables if they don't already exist. Safe to call on every startup."""
    import os
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency -- yields a session, guarantees it's closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()