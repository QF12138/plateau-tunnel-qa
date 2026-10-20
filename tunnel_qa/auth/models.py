from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tunnel_qa.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_accounts"
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    security_level: Mapped[str] = mapped_column(String(20), default="internal", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, index=True, nullable=False)


class AccountEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "account_events"
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
