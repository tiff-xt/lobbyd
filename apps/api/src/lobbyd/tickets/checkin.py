"""Scanning tickets at the door, online and offline (ADR 0006).

Online, admission is one conditional UPDATE: of two scanners racing on one
ticket, exactly one gets the row.

Offline, a scanner works from a manifest it downloaded earlier and uploads
its scans when the network returns. Scans are applied by the time they were
made, not the time they arrived: the earliest scan of a ticket is the
admission, and every other is recorded as a duplicate for the organizer.
"""

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lobbyd.catalog import service as catalog
from lobbyd.catalog.schemas import SeatLabel
from lobbyd.catalog.service import Managed
from lobbyd.platform import metrics
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.ids import uuid7
from lobbyd.tickets import tokens
from lobbyd.tickets.models import CheckinScan, Ticket
from lobbyd.tickets.tokens import InvalidTicketToken

log = structlog.get_logger()

MAX_BATCH = 500
# A phone's clock can drift. Further out than this and the time is not believed.
CLOCK_TOLERANCE = timedelta(minutes=2)
# No scanner stays offline this long. A scan dated earlier has a wrong clock.
MAX_OFFLINE = timedelta(days=3)


# --- shapes ---------------------------------------------------------------------


class KeyOut(BaseModel):
    key_id: str
    algorithm: str = "Ed25519"
    public_key: str = Field(description="Raw 32-byte public key, base64url")


class ManifestTicket(BaseModel):
    id: uuid.UUID
    ticket_type_name: str
    checked_in_at: datetime | None


class Manifest(BaseModel):
    """Everything a scanner needs to keep working without a network."""

    tournament_id: uuid.UUID
    title: str
    generated_at: datetime
    keys: list[KeyOut] = Field(description="Public keys that ticket tokens may be signed with")
    tickets: list[ManifestTicket] = Field(
        description="Every valid ticket. A ticket that is not listed is refused."
    )


class ScanIn(BaseModel):
    token: str = Field(max_length=400, description="The scanned QR code's contents")
    device_id: str = Field(min_length=1, max_length=64)


class ScanOut(BaseModel):
    scan_id: uuid.UUID
    result: str = Field(description="admitted, duplicate (already used), or void (cancelled)")
    ticket_id: uuid.UUID
    ticket_type_name: str
    seat: SeatLabel | None
    checked_in_at: datetime | None = Field(description="When the ticket was first admitted")


class OfflineScan(BaseModel):
    id: uuid.UUID = Field(description="Chosen by the scanner. Uploading it again changes nothing")
    token: str = Field(max_length=400)
    scanned_at: datetime = Field(description="The device's clock when it scanned")


class SyncIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    scans: list[OfflineScan] = Field(min_length=1, max_length=MAX_BATCH)


class SyncedScan(BaseModel):
    id: uuid.UUID
    result: str = Field(
        description="admitted, duplicate, void, or rejected (not a ticket for this event)"
    )
    clock_suspect: bool = False


class SyncOut(BaseModel):
    scans: list[SyncedScan]


class FlaggedScan(BaseModel):
    scan_id: uuid.UUID
    ticket_id: uuid.UUID
    result: str
    device_id: str
    scanned_at: datetime
    offline: bool
    clock_suspect: bool
    admitted_by: str | None = Field(description="The device whose scan counted as the admission")
    admitted_at: datetime | None


class Summary(BaseModel):
    tickets: int = Field(description="Valid tickets issued")
    checked_in: int
    duplicates: int = Field(description="Scans of a ticket that had already been admitted")
    flagged: list[FlaggedScan] = Field(description="Most recent duplicates and doubtful clocks")


# --- reading ---------------------------------------------------------------------


async def manifest(session: AsyncSession, managed: Managed) -> Manifest:
    rows = (
        await session.execute(
            select(Ticket.id, Ticket.ticket_type_id, Ticket.checked_in_at)
            .where(Ticket.tournament_id == managed.tournament_id, Ticket.status == "valid")
            .order_by(Ticket.id)
        )
    ).all()
    names = await catalog.ticket_type_names(session, list({row.ticket_type_id for row in rows}))
    signer = tokens.signer()
    return Manifest(
        tournament_id=managed.tournament_id,
        title=managed.title,
        generated_at=datetime.now(UTC),
        keys=[KeyOut(key_id=signer.key_id, public_key=signer.public_key)],
        tickets=[
            ManifestTicket(
                id=row.id,
                ticket_type_name=names[row.ticket_type_id],
                checked_in_at=row.checked_in_at,
            )
            for row in rows
        ],
    )


async def summary(session: AsyncSession, managed: Managed) -> Summary:
    issued, checked_in = (
        await session.execute(
            select(func.count(), func.count(Ticket.checked_in_at)).where(
                Ticket.tournament_id == managed.tournament_id, Ticket.status == "valid"
            )
        )
    ).one()
    duplicates = await session.scalar(
        select(func.count())
        .select_from(CheckinScan)
        .where(
            CheckinScan.tournament_id == managed.tournament_id, CheckinScan.result == "duplicate"
        )
    )
    admission = aliased(CheckinScan)
    flagged = await session.execute(
        select(CheckinScan, admission.device_id, admission.scanned_at)
        .outerjoin(
            admission,
            (admission.ticket_id == CheckinScan.ticket_id) & (admission.result == "admitted"),
        )
        .where(
            CheckinScan.tournament_id == managed.tournament_id,
            (CheckinScan.result == "duplicate") | CheckinScan.clock_suspect,
        )
        .order_by(CheckinScan.scanned_at.desc())
        .limit(100)
    )
    return Summary(
        tickets=issued,
        checked_in=checked_in,
        duplicates=int(duplicates or 0),
        flagged=[
            FlaggedScan(
                scan_id=scan.id,
                ticket_id=scan.ticket_id,
                result=scan.result,
                device_id=scan.device_id,
                scanned_at=scan.scanned_at,
                offline=scan.offline,
                clock_suspect=scan.clock_suspect,
                admitted_by=device if scan.result == "duplicate" else None,
                admitted_at=at if scan.result == "duplicate" else None,
            )
            for scan, device, at in flagged
        ],
    )


# --- scanning online -------------------------------------------------------------


def _claim(token: str, managed: Managed) -> uuid.UUID:
    try:
        claim = tokens.signer().verify(token)
    except InvalidTicketToken as exc:
        raise ProblemError(
            422, "invalid_ticket_token", "This is not a ticket issued by lobbyd."
        ) from exc
    if claim.tournament_id != managed.tournament_id:
        raise ProblemError(409, "wrong_tournament", "This ticket is for a different tournament.")
    return claim.ticket_id


async def scan(session: AsyncSession, managed: Managed, data: ScanIn) -> ScanOut:
    ticket_id = _claim(data.token, managed)

    # The whole race is decided here. Two scanners on one ticket: the second
    # UPDATE waits for the first, then finds checked_in_at set and matches nothing.
    admitted = (
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == "valid", Ticket.checked_in_at.is_(None))
            .values(checked_in_at=func.now())
            .returning(Ticket.ticket_type_id, Ticket.seat_id, Ticket.checked_in_at)
        )
    ).one_or_none()
    if admitted is not None:
        result = "admitted"
        ticket_type_id, seat_id, checked_in_at = admitted
    else:
        found = (
            await session.execute(
                select(
                    Ticket.ticket_type_id, Ticket.seat_id, Ticket.checked_in_at, Ticket.status
                ).where(Ticket.id == ticket_id)
            )
        ).one_or_none()
        if found is None:
            raise ProblemError(404, "ticket_not_found", "No such ticket.")
        ticket_type_id, seat_id, checked_in_at, status = found
        result = "void" if status == "void" else "duplicate"

    scan_id = uuid7()
    session.add(
        CheckinScan(
            id=scan_id,
            ticket_id=ticket_id,
            tournament_id=managed.tournament_id,
            device_id=data.device_id,
            scanned_at=func.now(),
            result=result,
        )
    )
    names = await catalog.ticket_type_names(session, [ticket_type_id])
    seats = await catalog.seat_labels(session, [seat_id]) if seat_id else {}
    metrics.SCANS.labels("online", result).inc()
    log.info("ticket_scanned", ticket_id=str(ticket_id), result=result, device=data.device_id)
    return ScanOut(
        scan_id=scan_id,
        result=result,
        ticket_id=ticket_id,
        ticket_type_name=names[ticket_type_id],
        seat=seats.get(seat_id) if seat_id else None,
        checked_in_at=checked_in_at,
    )


# --- syncing offline scans ---------------------------------------------------------


async def sync(session: AsyncSession, managed: Managed, data: SyncIn) -> SyncOut:
    """Apply a scanner's stored scans. Uploading the same batch again changes nothing."""
    now = datetime.now(UTC)
    results: dict[uuid.UUID, SyncedScan] = {}
    claimed: dict[uuid.UUID, uuid.UUID] = {}  # scan id -> ticket id
    for item in data.scans:
        try:
            claim = tokens.signer().verify(item.token)
        except InvalidTicketToken:
            claim = None
        if claim is None or claim.tournament_id != managed.tournament_id:
            results[item.id] = SyncedScan(id=item.id, result="rejected")
        else:
            claimed[item.id] = claim.ticket_id

    # Lock every ticket in the batch first, in id order, so two scanners
    # syncing overlapping tickets wait for each other and cannot deadlock.
    tickets = {
        ticket.id: ticket
        for ticket in await session.scalars(
            select(Ticket)
            .where(
                Ticket.id.in_(set(claimed.values())),
                Ticket.tournament_id == managed.tournament_id,
            )
            .order_by(Ticket.id)
            .with_for_update()
        )
    }
    # Only now, with the locks held, is "have we seen this scan?" a stable answer.
    already = {
        row.id: row
        for row in await session.execute(
            select(CheckinScan.id, CheckinScan.result, CheckinScan.clock_suspect).where(
                CheckinScan.id.in_([item.id for item in data.scans])
            )
        )
    }

    # In the order the scans happened, whatever order they were uploaded in.
    for item in sorted(data.scans, key=lambda scan: (scan.scanned_at, scan.id)):
        if item.id in results:
            continue
        if item.id in already:
            seen = already[item.id]
            results[item.id] = SyncedScan(
                id=item.id, result=seen.result, clock_suspect=seen.clock_suspect
            )
            continue
        ticket = tickets.get(claimed[item.id])
        if ticket is None:
            results[item.id] = SyncedScan(id=item.id, result="rejected")
            continue

        scanned_at = item.scanned_at.astimezone(UTC)
        suspect = scanned_at > now + CLOCK_TOLERANCE or scanned_at < now - MAX_OFFLINE
        # A clock we do not believe cannot be used to order scans. Such a
        # scan counts from when it reached us.
        effective = now if suspect else min(scanned_at, now)

        if ticket.status == "void":
            result = "void"
        elif ticket.checked_in_at is None:
            result = "admitted"
            ticket.checked_in_at = effective
        elif effective < ticket.checked_in_at:
            # This scan happened before the one we had counted. It is the
            # real admission; the one recorded earlier becomes the duplicate.
            await session.execute(
                update(CheckinScan)
                .where(CheckinScan.ticket_id == ticket.id, CheckinScan.result == "admitted")
                .values(result="duplicate")
            )
            result = "admitted"
            ticket.checked_in_at = effective
        else:
            result = "duplicate"
        await session.flush()
        await session.execute(
            pg_insert(CheckinScan)
            .values(
                id=item.id,
                ticket_id=ticket.id,
                tournament_id=managed.tournament_id,
                device_id=data.device_id,
                scanned_at=effective,
                result=result,
                offline=True,
                clock_suspect=suspect,
            )
            .on_conflict_do_nothing(index_elements=[CheckinScan.id])
        )
        results[item.id] = SyncedScan(id=item.id, result=result, clock_suspect=suspect)

    tally = Counter(scan.result for scan in results.values())
    for result, count in tally.items():
        metrics.SCANS.labels("offline", result).inc(count)
    log.info("scans_synced", device=data.device_id, total=len(data.scans), **tally)
    return SyncOut(scans=[results[item.id] for item in data.scans])
