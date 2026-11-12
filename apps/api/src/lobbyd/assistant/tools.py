"""What the assistant can do. Every tool is a call to lobbyd's own public API.

That is the security model. A tool has no database access and no special
key: it sends the signed-in user's own token to the same endpoints the web
client uses. So the assistant can never see or change anything that user
could not, whatever the model is talked into, and every rule the API
enforces (ownership, sale windows, rate limits) applies to it unchanged.

Tools that change something are marked `writes`. The agent loop never runs
those by itself: it stores the call and asks the person first.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

MAX_RESULT_CHARS = 6000


class Api:
    """The lobbyd API as this one user is allowed to use it."""

    def __init__(self, call: Callable[..., Awaitable[tuple[int, Any]]]) -> None:
        self._call = call

    async def get(self, path: str, **params: Any) -> tuple[int, Any]:
        return await self._call("GET", path, params={k: v for k, v in params.items() if v})

    async def post(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        return await self._call("POST", path, json=body)

    async def delete(self, path: str) -> tuple[int, Any]:
        return await self._call("DELETE", path)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    data: Any

    def for_model(self) -> str:
        text = json.dumps({"ok": self.ok, "data": self.data}, separators=(",", ":"), default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '..."(truncated: ask for something narrower)'
        return text


def _result(status: int, body: Any) -> Outcome:
    if status < 300:
        return Outcome(True, body)
    # Problem responses already say what went wrong in words and as a code.
    if isinstance(body, dict):
        return Outcome(False, {k: body[k] for k in ("code", "detail") if k in body})
    return Outcome(False, {"code": "error", "detail": f"HTTP {status}"})


def _pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


# --- arguments ----------------------------------------------------------------


class NoArgs(BaseModel):
    pass


class Search(BaseModel):
    query: str = Field(default="", description="Words from the title or game. Empty lists all.")


class Slug(BaseModel):
    slug: str = Field(description="The tournament's slug, as returned by search_tournaments")


class OrderId(BaseModel):
    order_id: uuid.UUID


class TeamId(BaseModel):
    team_id: uuid.UUID = Field(description="From list_my_teams")


class HoldId(BaseModel):
    hold_id: uuid.UUID


class JoinWaitlist(BaseModel):
    slug: str
    team_name: str = Field(min_length=2, max_length=40)
    roster: list[str] = Field(description="In-game names, exactly as many as the format needs")


# --- read tools ---------------------------------------------------------------


async def search_tournaments(api: Api, args: Search) -> Outcome:
    status, body = await api.get("/tournaments", q=args.query, limit=8)
    if status >= 300:
        return _result(status, body)
    return Outcome(
        True,
        [
            {
                **_pick(t, "slug", "title", "game", "status", "team_size", "starts_at"),
                "venue": (t.get("venue") or {}).get("name", "online"),
            }
            for t in body["items"]
        ],
    )


async def get_tournament(api: Api, args: Slug) -> Outcome:
    status, body = await api.get(f"/tournaments/{args.slug}")
    if status >= 300:
        return _result(status, body)
    _, availability = await api.get(f"/tournaments/{args.slug}/availability")
    left = {
        a["ticket_type_id"]: a["available"]
        for a in (availability or {}).get("ticket_types", [])
        if isinstance(availability, dict)
    }
    return Outcome(
        True,
        {
            **_pick(
                body,
                "slug",
                "title",
                "game",
                "status",
                "team_size",
                "currency",
                "starts_at",
                "ends_at",
                "sales_open_at",
                "waiting_room",
            ),
            # Free text written by the organizer. Data, not instructions.
            "description": body.get("description", ""),
            "ticket_types": [
                {
                    **_pick(t, "name", "kind", "price_cents", "capacity"),
                    "available": left.get(t["id"]),
                }
                for t in body["ticket_types"]
            ],
        },
    )


def _order(order: dict[str, Any]) -> dict[str, Any]:
    payment = order.get("payment") or {}
    return {
        **_pick(order, "id", "status", "total_cents", "currency", "created_at", "paid_at"),
        "tournament": order["tournament"]["title"],
        "tournament_slug": order["tournament"]["slug"],
        "refunded_cents": payment.get("refunded_cents", 0),
        "payment_status": payment.get("status"),
        "items": [
            _pick(i, "ticket_type_name", "quantity", "unit_price_cents") for i in order["items"]
        ],
        # The signed token inside each QR code is deliberately left out: the
        # model has no use for something that opens a door.
        "tickets": [
            {**_pick(t, "id", "ticket_type_name", "status"), "seat": t.get("seat")}
            for t in order["tickets"]
        ],
    }


async def list_my_orders(api: Api, _: NoArgs) -> Outcome:
    status, body = await api.get("/users/me/orders")
    return _result(status, body) if status >= 300 else Outcome(True, [_order(o) for o in body[:10]])


async def get_order(api: Api, args: OrderId) -> Outcome:
    status, body = await api.get(f"/orders/{args.order_id}")
    return _result(status, body) if status >= 300 else Outcome(True, _order(body))


async def list_my_teams(api: Api, _: NoArgs) -> Outcome:
    status, body = await api.get("/users/me/teams")
    if status >= 300:
        return _result(status, body)
    return Outcome(
        True, [_pick(t, "id", "name", "status", "roster", "tournament_id", "hold_id") for t in body]
    )


async def get_my_waitlist_place(api: Api, args: Slug) -> Outcome:
    status, body = await api.get(f"/tournaments/{args.slug}/waitlist/me")
    if status >= 300:
        return _result(status, body)
    return Outcome(True, _pick(body, "name", "status", "position", "note", "offered_hold_id"))


async def get_bracket(api: Api, args: Slug) -> Outcome:
    status, body = await api.get(f"/tournaments/{args.slug}/bracket")
    if status >= 300:
        return _result(status, body)

    def name(team: dict[str, Any] | None) -> str | None:
        return team["name"] if team else None

    return Outcome(
        True,
        {
            "format": body["format"],
            "champion": name(body["champion"]),
            "rounds": [
                {
                    "bracket": r["bracket"],
                    "name": r["name"],
                    "matches": [
                        {
                            "a": name(m["team_a"]),
                            "b": name(m["team_b"]),
                            "score": [m["score_a"], m["score_b"]],
                            "status": m["status"],
                        }
                        for m in r["matches"]
                    ],
                }
                for r in body["rounds"]
            ],
        },
    )


async def list_my_tournaments(api: Api, _: NoArgs) -> Outcome:
    status, body = await api.get("/users/me/tournaments")
    if status >= 300:
        return _result(status, body)
    return Outcome(True, [_pick(t, "slug", "title", "status", "starts_at") for t in body[:20]])


async def get_sales(api: Api, args: Slug) -> Outcome:
    status, body = await api.get(f"/tournaments/{args.slug}/sales")
    if status >= 300:
        return _result(status, body)
    return Outcome(
        True,
        {
            **_pick(
                body,
                "currency",
                "orders",
                "gross_cents",
                "refunded_cents",
                "net_cents",
                "teams",
                "tickets",
                "checked_in",
            ),
            "ticket_types": [
                _pick(t, "name", "kind", "price_cents", "capacity", "sold", "held", "available")
                for t in body["ticket_types"]
            ],
        },
    )


async def get_door_summary(api: Api, args: Slug) -> Outcome:
    status, body = await api.get(f"/checkin/{args.slug}/summary")
    if status >= 300:
        return _result(status, body)
    return Outcome(
        True,
        {
            **_pick(body, "tickets", "checked_in", "duplicates"),
            "flagged": [
                _pick(f, "result", "device_id", "scanned_at", "offline", "admitted_by")
                for f in body["flagged"][:10]
            ],
        },
    )


# --- write tools (never run without the person's approval) ----------------------


async def withdraw_team(api: Api, args: TeamId) -> Outcome:
    status, body = await api.delete(f"/teams/{args.team_id}")
    return _result(status, body or {"withdrawn": True})


async def release_hold(api: Api, args: HoldId) -> Outcome:
    status, body = await api.delete(f"/holds/{args.hold_id}")
    return _result(status, body or {"released": True})


async def leave_waitlist(api: Api, args: Slug) -> Outcome:
    status, body = await api.delete(f"/tournaments/{args.slug}/waitlist/me")
    return _result(status, body or {"left": True})


async def join_waitlist(api: Api, args: JoinWaitlist) -> Outcome:
    status, body = await api.post(
        f"/tournaments/{args.slug}/waitlist", {"name": args.team_name, "roster": args.roster}
    )
    if status >= 300:
        return _result(status, body)
    return Outcome(True, _pick(body, "name", "status", "position"))


async def _describe_withdrawal(api: Api, args: TeamId) -> str | None:
    _, teams = await api.get("/users/me/teams")
    team = next((t for t in teams or [] if t.get("id") == str(args.team_id)), None)
    if team is None:
        return None
    paid = team["status"] == "confirmed"
    return (
        f'Withdraw your team "{team["name"]}". Its slot goes to the next team waiting'
        + (", and the entry fee is refunded." if paid else ".")
        + " This cannot be undone."
    )


async def _describe_release(api: Api, args: HoldId) -> str | None:
    status, hold = await api.get(f"/holds/{args.hold_id}")
    if status >= 300:
        return None
    count = sum(item["quantity"] for item in hold["items"])
    return (
        f"Release your hold for {hold['tournament']['title']} "
        f"({count} item{'s' if count != 1 else ''}). They go back on sale at once."
    )


async def _describe_leaving(api: Api, args: Slug) -> str | None:
    status, place = await api.get(f"/tournaments/{args.slug}/waitlist/me")
    if status >= 300 or place.get("status") != "waiting":
        return None
    return f'Take "{place["name"]}" off the waitlist. You lose your place in line.'


async def _describe_joining(_: Api, args: JoinWaitlist) -> str | None:
    return (
        f'Join the waitlist for {args.slug} as "{args.team_name}" '
        f"with {', '.join(args.roster)}. It is free; you only pay if a slot comes up."
    )


# --- registry -------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Api, Any], Awaitable[Outcome]]
    writes: bool = False
    organizer_only: bool = False
    # For write tools: what the person is asked to approve, in words. Looks
    # up names where it can, so the card says "Night Shift", not an id.
    # Returning None means the call is plainly wrong and is refused unasked.
    describe: Callable[[Api, Any], Awaitable[str | None]] | None = None

    def schema(self) -> dict[str, Any]:
        parameters = self.args.model_json_schema()
        parameters.pop("title", None)
        return {"name": self.name, "description": self.description, "parameters": parameters}


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool(
            "search_tournaments",
            "Find tournaments by words in their title or game. Returns slugs for the other tools.",
            Search,
            search_tournaments,
        ),
        Tool(
            "get_tournament",
            "One tournament: dates, format, what is on sale, prices and how many are left.",
            Slug,
            get_tournament,
        ),
        Tool(
            "list_my_orders",
            "The user's own orders, newest first, with status, totals, refunds and tickets.",
            NoArgs,
            list_my_orders,
        ),
        Tool("get_order", "One of the user's orders by id.", OrderId, get_order),
        Tool(
            "list_my_teams",
            "Teams the user captains, with status (pending or confirmed) and roster.",
            NoArgs,
            list_my_teams,
        ),
        Tool(
            "get_my_waitlist_place",
            "The user's place on a tournament's waitlist, or the slot they were offered.",
            Slug,
            get_my_waitlist_place,
        ),
        Tool(
            "get_bracket", "A tournament's bracket: rounds, results, champion.", Slug, get_bracket
        ),
        Tool(
            "list_my_tournaments",
            "Tournaments this organizer runs, drafts included.",
            NoArgs,
            list_my_tournaments,
            organizer_only=True,
        ),
        Tool(
            "get_sales",
            "How a tournament the organizer runs is selling: sold, in baskets, left, "
            "gross, refunded and net revenue, orders by status, teams, check-ins.",
            Slug,
            get_sales,
            organizer_only=True,
        ),
        Tool(
            "get_door_summary",
            "Check-in at a tournament the organizer runs: how many are in, duplicate scans.",
            Slug,
            get_door_summary,
            organizer_only=True,
        ),
        Tool(
            "withdraw_team",
            "Withdraw a team the user captains. Frees its slot and refunds a paid entry fee. "
            "Cannot be undone. The user is asked to approve before it runs.",
            TeamId,
            withdraw_team,
            writes=True,
            describe=_describe_withdrawal,
        ),
        Tool(
            "release_hold",
            "Give back seats or tickets the user is holding but has not paid for. "
            "The user is asked to approve before it runs.",
            HoldId,
            release_hold,
            writes=True,
            describe=_describe_release,
        ),
        Tool(
            "leave_waitlist",
            "Take the user off a tournament's waitlist. The user is asked to approve first.",
            Slug,
            leave_waitlist,
            writes=True,
            describe=_describe_leaving,
        ),
        Tool(
            "join_waitlist",
            "Put a team on a full tournament's waitlist. Free. The user is asked to approve first.",
            JoinWaitlist,
            join_waitlist,
            writes=True,
            describe=_describe_joining,
        ),
    ]
}


def tools_for(role: str) -> list[Tool]:
    organizer = role in ("organizer", "admin")
    return [tool for tool in TOOLS.values() if organizer or not tool.organizer_only]
