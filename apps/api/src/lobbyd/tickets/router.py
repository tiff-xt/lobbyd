"""HTTP endpoints for the door: manifest, scanning, and the organizer's summary."""

from typing import Annotated

from fastapi import APIRouter, Depends

from lobbyd.catalog import service as catalog
from lobbyd.catalog.service import Managed
from lobbyd.identity.deps import CurrentUser
from lobbyd.platform.db import SessionDep
from lobbyd.tickets import checkin
from lobbyd.tickets.checkin import Manifest, ScanIn, ScanOut, Summary, SyncIn, SyncOut

router = APIRouter(prefix="/checkin/{slug}", tags=["check-in"])


async def _managed(slug: str, session: SessionDep, user: CurrentUser) -> Managed:
    """Scanning is for the tournament's organizer (and admins)."""
    return await catalog.require_owner(session, user, slug=slug)


ManagedDep = Annotated[Managed, Depends(_managed)]


@router.get(
    "/manifest",
    summary="Everything a scanner needs to work offline",
    description=(
        "The public key that ticket tokens are signed with, and every valid ticket with "
        "whether it has been used. Download it before doors open and again whenever "
        "there is a connection."
    ),
)
async def get_manifest(managed: ManagedDep, session: SessionDep) -> Manifest:
    return await checkin.manifest(session, managed)


@router.post(
    "/scan",
    summary="Scan one ticket, online",
    description=(
        "`admitted` exactly once per ticket, however many scanners try at the same "
        "moment. After that, `duplicate` with the time it was first used."
    ),
)
async def scan_ticket(body: ScanIn, managed: ManagedDep, session: SessionDep) -> ScanOut:
    return await checkin.scan(session, managed, body)


@router.post(
    "/scans",
    summary="Upload scans made while offline",
    description=(
        "Scans are applied in the order they were made. The earliest scan of a ticket is "
        "the admission; the rest are recorded as duplicates. Each scan has an id chosen "
        "by the scanner, so uploading a batch twice is harmless."
    ),
)
async def sync_scans(body: SyncIn, managed: ManagedDep, session: SessionDep) -> SyncOut:
    return await checkin.sync(session, managed, body)


@router.get("/summary", summary="How many are in, and which scans need a look")
async def get_summary(managed: ManagedDep, session: SessionDep) -> Summary:
    return await checkin.summary(session, managed)
