"""What the assistant said and did, kept so every answer can be traced."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.ids import uuid7


class Conversation(Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (Index("assistant_conversations_user_idx", "user_id", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class Message(Base):
    """One entry in a conversation, in the order the model saw them.

    role 'user':      what the person typed
    role 'assistant': what the model said, and the tools it asked for
    role 'tool':      what a tool returned (or why it did not run)
    """

    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'tool')", name="role"),
        UniqueConstraint("conversation_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, server_default="")
    # assistant: [{"id", "name", "arguments"}] it asked for.
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # tool: which request this answers, and how it went.
    tool_call_id: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(Text)
    # Measurements, for the trace: model, tokens, milliseconds, flags.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class Action(Base):
    """Something the assistant wants to do that changes things, waiting on a person.

    The exact call is stored when it is proposed. Approving it runs that
    stored call, not whatever the model says next, so what the person agreed
    to is what happens.
    """

    __tablename__ = "assistant_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'executed', 'failed', 'rejected', 'superseded')", name="status"
        ),
        Index(
            "assistant_actions_pending_idx",
            "conversation_id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tool_call_id: Mapped[str] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text)  # in words, for the person deciding
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
