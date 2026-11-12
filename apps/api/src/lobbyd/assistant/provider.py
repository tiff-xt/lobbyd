"""The language model behind the assistant, behind one small interface.

The agent loop knows nothing about any provider. It hands over a system
prompt, the conversation so far, and the tools on offer, and gets back text
and/or requests to call tools. That keeps the model replaceable (free tiers
come and go) and lets tests run the whole loop against a scripted stand-in.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from lobbyd.config import Settings


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model's arguments were not valid JSON: the loop tells it so.
    malformed: str | None = None


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class ModelError(Exception):
    """The provider could not be reached, refused, or answered nonsense."""


class Provider(Protocol):
    name: str

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...

    async def aclose(self) -> None: ...


class ChatCompletionsProvider:
    """Any provider that speaks the widely copied chat-completions format with tools.

    `messages` arrive in that format already (it is also how they are stored):
    {"role": "user" | "assistant" | "tool", "content": ..., "tool_calls": ...}.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.name = settings.assistant_model
        self._model = settings.assistant_model
        key = settings.assistant_api_key.get_secret_value()
        self._client = client or httpx.AsyncClient(
            base_url=settings.assistant_base_url.rstrip("/"),
            # A local model server needs no key.
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=settings.assistant_timeout_seconds,
        )

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": [{"type": "function", "function": tool} for tool in tools],
            "tool_choice": "auto",
            "temperature": 0,
        }
        try:
            response = await self._client.post("/chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            raise ModelError(f"provider answered {exc.response.status_code}") from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ModelError(type(exc).__name__) from exc

        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            raw = function.get("arguments") or "{}"
            arguments: dict[str, Any] = {}
            malformed = None
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    malformed = "arguments must be a JSON object"
            except ValueError:
                malformed = "arguments were not valid JSON"
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    malformed=malformed,
                )
            )
        usage = data.get("usage") or {}
        return ModelTurn(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class ScriptedProvider:
    """A stand-in that plays back prepared turns. For tests of everything but the model.

    Each item is a ModelTurn, or a function of the messages so far that
    returns one (so a script can react to what a tool returned).
    """

    name = "scripted"

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []
        self.systems: list[str] = []

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.seen.append(messages)
        self.systems.append(system)
        if not self.turns:
            raise ModelError("the script ran out of turns")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        result: ModelTurn = turn(messages) if callable(turn) else turn
        return result

    async def aclose(self) -> None:
        return None


def from_settings(settings: Settings) -> Provider | None:
    if not (settings.assistant_base_url and settings.assistant_model):
        return None
    return ChatCompletionsProvider(settings)
