import pytest
from cryptography.fernet import Fernet

from tunnel_qa.core.config import Settings
from tunnel_qa.db.session import Database


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        original_content_database_url=f"sqlite:///{tmp_path / 'original_documents.db'}",
        chunk_content_database_url=f"sqlite:///{tmp_path / 'document_chunks.db'}",
        storage_path=tmp_path / "storage",
        chunk_size=200,
        chunk_overlap=20,
        public_access_enabled=False,
        trust_identity_headers=True,
        secrets_encryption_key=Fernet.generate_key().decode(),
    )


@pytest.fixture()
def database(settings):
    database = Database(settings)
    database.create_schema()
    yield database
    database.drop_schema()


@pytest.fixture()
def db_session(database):
    with next(database.session()) as session:
        yield session
