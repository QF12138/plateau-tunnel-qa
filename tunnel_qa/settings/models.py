from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tunnel_qa.db.base import Base, utc_now


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="system")
    model_mode: Mapped[str] = mapped_column(String(20), default="local", nullable=False)
    api_provider: Mapped[str] = mapped_column(String(40), default="deepseek", nullable=False)
    api_base_url: Mapped[str] = mapped_column(
        String(500), default="https://api.deepseek.com", nullable=False
    )
    api_model: Mapped[str] = mapped_column(String(120), default="deepseek-chat", nullable=False)
    api_key_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_provider: Mapped[str] = mapped_column(String(30), default="builtin", nullable=False)
    local_base_url: Mapped[str] = mapped_column(
        String(500), default="http://127.0.0.1:11434", nullable=False
    )
    local_model: Mapped[str] = mapped_column(String(120), default="qwen2.5:7b", nullable=False)
    llm_temperature: Mapped[float] = mapped_column(Float, default=0.1, nullable=False)
    llm_timeout_seconds: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    retrieval_top_k: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    minimum_evidence_score: Mapped[float] = mapped_column(Float, default=0.12, nullable=False)
    default_response_mode: Mapped[str] = mapped_column(
        String(20), default="detailed", nullable=False
    )
    auto_report: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    report_format: Mapped[str] = mapped_column(String(20), default="markdown", nullable=False)
    theme: Mapped[str] = mapped_column(String(20), default="light", nullable=False)
    density: Mapped[str] = mapped_column(String(20), default="comfortable", nullable=False)
    audit_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    safety_review_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
