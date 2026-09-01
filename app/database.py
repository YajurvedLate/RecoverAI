from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Relative URL: recoverai.db in the project root when the app is started from RecoverAI/.
SQLALCHEMY_DATABASE_URL = "sqlite:///./recoverai.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # SQLite allows one thread per connection by default; FastAPI can use
    # the same session across more than one thread.
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
