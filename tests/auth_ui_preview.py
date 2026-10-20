"""Isolated localhost browser fixture; never uses production data or API keys."""
from pathlib import Path
from tempfile import mkdtemp

from tunnel_qa.app import create_app
from tunnel_qa.core.config import Settings

root = Path(mkdtemp(prefix="tunnel-auth-ui-"))
settings = Settings(_env_file=None, database_url=f"sqlite:///{root / 'main.db'}",
    original_content_database_url=f"sqlite:///{root / 'original.db'}",
    chunk_content_database_url=f"sqlite:///{root / 'chunks.db'}", storage_path=root / "storage",
    deepseek_api_key=None, model_mode="local", public_access_enabled=True,
    public_access_username="preview-admin", public_access_password="Preview-Admin-2026",
    public_reader_password=None, environment="test")
app = create_app(settings)
