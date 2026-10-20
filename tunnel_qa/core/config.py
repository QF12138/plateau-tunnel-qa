from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tunnel_qa.core.enums import SecurityLevel


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="TUNNEL_QA_",
        env_file=".env",
        populate_by_name=True,
        extra="ignore",
    )

    app_name: str = "高原铁路隧道科研-施工智能问答系统"
    app_version: str = "0.1.0"
    environment: str = "development"
    database_url: str = "sqlite:///./data/tunnel_qa.db"
    original_content_database_url: str = "sqlite:///./data/original_documents.db"
    chunk_content_database_url: str = "sqlite:///./data/document_chunks.db"
    storage_path: Path = Path("./data/storage")
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    chunk_size: int = Field(default=800, ge=100, le=5000)
    chunk_overlap: int = Field(default=120, ge=0, le=1000)
    retrieval_top_k: int = Field(default=8, ge=1, le=100)
    model_mode: Literal["auto", "local", "api"] = "auto"
    api_provider: Literal["deepseek", "openai_compatible"] = "deepseek"
    local_provider: Literal["builtin", "ollama"] = "builtin"
    local_base_url: str = "http://127.0.0.1:11434"
    local_model: str = "qwen2.5:7b"
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "TUNNEL_QA_DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "TUNNEL_QA_DEEPSEEK_BASE_URL"),
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "TUNNEL_QA_DEEPSEEK_MODEL"),
    )
    llm_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    llm_temperature: float = Field(default=0.1, ge=0, le=1)
    prompt_version: str = "qa-evidence-v1"
    default_response_mode: Literal["detailed", "brief"] = "detailed"
    minimum_evidence_score: float = Field(default=0.12, ge=0, le=1)
    public_access_enabled: bool = False
    public_access_username: str = Field(default="tunnel-admin", min_length=3, max_length=100)
    public_access_password: SecretStr | None = None
    public_access_user_id: str = Field(default="web-demo", min_length=1, max_length=100)
    public_reader_username: str = Field(default="tunnel-reader", min_length=3, max_length=100)
    public_reader_password: SecretStr | None = None
    public_reader_security_level: SecurityLevel = SecurityLevel.INTERNAL
    trust_identity_headers: bool = False
    secrets_encryption_key: SecretStr | None = None

    def prepare_directories(self) -> None:
        self.storage_path.mkdir(parents=True, exist_ok=True)
        for database_url in (
            self.database_url,
            self.original_content_database_url,
            self.chunk_content_database_url,
        ):
            if database_url.startswith("sqlite:///"):
                database_path = database_url.removeprefix("sqlite:///")
                if database_path != ":memory:":
                    Path(database_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
