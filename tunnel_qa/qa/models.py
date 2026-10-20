from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tunnel_qa.core.enums import AnswerType, SecurityLevel
from tunnel_qa.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    owner_user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(String(20), nullable=False)
    scenario: Mapped[str | None] = mapped_column(String(50))
    context_filters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.sequence"
    )


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type: Mapped[AnswerType | None] = mapped_column(String(30))
    citations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    graph_path: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class QATrace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "qa_traces"

    trace_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_question: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    retrieved_chunk_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    knowledge_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refusal_reason: Mapped[str | None] = mapped_column(String(200))

