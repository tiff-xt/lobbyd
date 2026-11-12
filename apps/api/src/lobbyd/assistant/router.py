"""HTTP endpoints for the assistant. Turns are streamed as server-sent events."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport
from pydantic import BaseModel, Field
from sqlalchemy import select

from lobbyd.assistant import agent
from lobbyd.assistant.models import Action, Conversation, Message
from lobbyd.assistant.provider import Provider
from lobbyd.assistant.tools import Api, tools_for
from lobbyd.identity.deps import CurrentUser, limit_per_user
from lobbyd.identity.models import User
from lobbyd.platform.cache import RedisDep
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.ids import uuid7
from lobbyd.platform.ratelimit import Limit

router = APIRouter(prefix="/assistant", tags=["assistant"])
log = structlog.get_logger()

# A turn costs model tokens. Enough for a conversation, not for a script.
TURNS = Limit("assistant", capacity=8, per_second=1 / 15)
_limited = [Depends(limit_per_user(TURNS))]
TURN_LOCK_SECONDS = 120


class StatusOut(BaseModel):
    enabled: bool = Field(description="False when no language model is configured")
    model: str | None
    tools: list[str] = Field(description="What the assistant can do for this user")
    suggestions: list[str]


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime


class MessageOut(BaseModel):
    seq: int
    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None
    tool_name: str | None
    meta: dict[str, Any]
    created_at: datetime


class ActionOut(BaseModel):
    id: uuid.UUID
    tool: str
    summary: str
    status: str


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
    pending_action: ActionOut | None


class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DecisionIn(BaseModel):
    approve: bool


def _provider(request: Request) -> Provider:
    provider: Provider | None = request.app.state.assistant
    if provider is None:
        raise ProblemError(
            503, "assistant_not_configured", "The assistant is not switched on for this site."
        )
    return provider


def _context(request: Request, user: User, provider: Provider) -> tuple[agent.Context, Any]:
    """A turn's context, with an API client that carries this user's own token."""
    client = httpx.AsyncClient(
        transport=ASGITransport(app=request.app),
        base_url="http://lobbyd.internal",
        headers={"Authorization": request.headers.get("authorization", "")},
        timeout=20,
    )

    async def call(method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        response = await client.request(method, path, **kwargs)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        return response.status_code, body

    settings = request.app.state.settings
    context = agent.Context(
        sessionmaker=request.app.state.sessionmaker,
        provider=provider,
        api=Api(call),
        user_id=user.id,
        user_name=user.display_name,
        role=user.role,
        max_steps=settings.assistant_max_steps,
    )
    return context, client


async def _own(session: SessionDep, user: User, conversation_id: uuid.UUID) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user.id
        )
    )
    if conversation is None:
        raise ProblemError(404, "conversation_not_found", "No such conversation.")
    return conversation


async def _budget(redis: RedisDep, settings: SettingsDep) -> None:
    """One counter for everyone, per day: a free model quota is a shared resource."""
    key = f"assistant:turns:{datetime.now(UTC):%Y%m%d}"
    used = await redis.incr(key)
    if used == 1:
        await redis.expire(key, 2 * 24 * 3600)
    if used > settings.assistant_daily_turns:
        raise ProblemError(
            503,
            "assistant_busy",
            "The assistant has answered all it can today. Everything else on the site works.",
        )


def _stream(
    events: AsyncIterator[agent.Event], client: httpx.AsyncClient, redis: Any, lock: str
) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        try:
            async for event in events:
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
        except Exception:
            log.exception("assistant_turn_failed")
            yield 'data: {"type":"error","code":"internal_error"}\n\n'
        finally:
            yield 'data: {"type":"done"}\n\n'
            await client.aclose()
            await redis.delete(lock)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        # Tell proxies not to hold the stream back to compress or batch it.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _begin(redis: RedisDep, conversation_id: uuid.UUID) -> str:
    """One turn at a time per conversation: two at once would interleave their steps."""
    lock = f"assistant:turn:{conversation_id}"
    if not await redis.set(lock, "1", nx=True, ex=TURN_LOCK_SECONDS):
        raise ProblemError(409, "turn_in_progress", "The assistant is still answering.")
    return lock


@router.get("", summary="Whether the assistant is available, and what it can do")
async def status(request: Request, user: CurrentUser) -> StatusOut:
    provider: Provider | None = request.app.state.assistant
    organizer = user.role in ("organizer", "admin")
    return StatusOut(
        enabled=provider is not None,
        model=provider.name if provider else None,
        tools=[tool.name for tool in tools_for(user.role)],
        suggestions=(
            [
                "How are my tournaments selling?",
                "Which of my events has the most refunds?",
                "How many people have checked in?",
            ]
            if organizer
            else [
                "Where are my tickets?",
                "Was I refunded for the team I withdrew?",
                "What is on this month, and what does it cost?",
                "Withdraw my team",
            ]
        ),
    )


@router.post("/conversations", status_code=201, summary="Start a conversation")
async def create_conversation(session: SessionDep, user: CurrentUser) -> ConversationOut:
    conversation = Conversation(id=uuid7(), user_id=user.id)
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    return ConversationOut(
        id=conversation.id, title=conversation.title, created_at=conversation.created_at
    )


@router.get("/conversations", summary="My recent conversations")
async def list_conversations(session: SessionDep, user: CurrentUser) -> list[ConversationOut]:
    rows = await session.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.id.desc())
        .limit(20)
    )
    return [ConversationOut(id=c.id, title=c.title, created_at=c.created_at) for c in rows]


@router.get(
    "/conversations/{conversation_id}",
    summary="A conversation, with every step the assistant took",
)
async def get_conversation(
    conversation_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> ConversationDetail:
    conversation = await _own(session, user, conversation_id)
    messages = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    pending = await session.scalar(
        select(Action).where(Action.conversation_id == conversation_id, Action.status == "pending")
    )
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        messages=[
            MessageOut(
                seq=m.seq,
                role=m.role,
                content=m.content,
                tool_calls=m.tool_calls,
                tool_name=m.tool_name,
                meta=m.meta,
                created_at=m.created_at,
            )
            for m in messages
        ],
        pending_action=(
            ActionOut(id=pending.id, tool=pending.tool, summary=pending.summary, status="pending")
            if pending
            else None
        ),
    )


_SSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/event-stream": {}}, "description": "Events, one per step"}
}


@router.post(
    "/conversations/{conversation_id}/messages",
    dependencies=_limited,
    responses=_SSE,
    summary="Say something to the assistant",
    description=(
        "Streams events as the assistant works: `assistant` (what it asked for), `tool` "
        "(what ran), `action` (a change waiting for your approval), `final` (its answer), "
        "then `done`. The assistant can only do what you could do yourself."
    ),
)
async def say(
    conversation_id: uuid.UUID,
    body: SayIn,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    user: CurrentUser,
) -> StreamingResponse:
    provider = _provider(request)
    await _own(session, user, conversation_id)
    await _budget(redis, settings)
    lock = await _begin(redis, conversation_id)
    context, client = _context(request, user, provider)
    return _stream(agent.say(context, conversation_id, body.text), client, redis, lock)


@router.post(
    "/actions/{action_id}/decision",
    responses=_SSE,
    summary="Approve or decline a change the assistant proposed",
    description=(
        "Approving runs exactly the call that was shown, with your own permissions. "
        "The assistant then reports what happened, as a stream of events."
    ),
)
async def decide(
    action_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    user: CurrentUser,
) -> StreamingResponse:
    provider = _provider(request)
    action = await session.scalar(
        select(Action).where(Action.id == action_id, Action.user_id == user.id)
    )
    if action is None:
        raise ProblemError(404, "action_not_found", "No such proposed change.")
    if action.status != "pending":
        raise ProblemError(409, "action_not_pending", "That change was already decided.")
    lock = await _begin(redis, action.conversation_id)
    context, client = _context(request, user, provider)
    return _stream(agent.decide(context, action_id, approve=body.approve), client, redis, lock)
