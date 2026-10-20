from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tunnel_qa.core.config import Settings, get_settings
from tunnel_qa.db.base import Base


def create_database_engine(database_url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 60}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA busy_timeout = 60000")
            finally:
                cursor.close()
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.prepare_directories()
        self.engine = create_database_engine(self.settings.database_url)
        self.session_factory = create_session_factory(self.engine)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_schema(self) -> None:
        Base.metadata.drop_all(self.engine)

    def session(self) -> Iterator[Session]:
        db_session = self.session_factory()
        try:
            yield db_session
        finally:
            db_session.close()
