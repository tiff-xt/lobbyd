"""HTTP endpoints for accounts and sessions."""

from fastapi import APIRouter, status

from lobbyd.identity import service
from lobbyd.identity.deps import CurrentUser
from lobbyd.identity.schemas import (
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
    UserOut,
)
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep

router = APIRouter()


@router.post(
    "/auth/signup",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
    summary="Create an account",
)
async def signup(body: SignupRequest, session: SessionDep) -> object:
    return await service.create_user(
        session, email=body.email, display_name=body.display_name, password=body.password
    )


@router.post("/auth/login", tags=["auth"], summary="Exchange email and password for tokens")
async def login(body: LoginRequest, session: SessionDep, settings: SettingsDep) -> TokenPair:
    return await service.login(session, settings, email=body.email, password=body.password)


@router.post(
    "/auth/refresh",
    tags=["auth"],
    summary="Exchange a refresh token for a new pair",
    description="Refresh tokens are single use. Each call returns a replacement.",
)
async def refresh(body: RefreshRequest, session: SessionDep, settings: SettingsDep) -> TokenPair:
    return await service.refresh(session, settings, refresh_token=body.refresh_token)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["auth"],
    summary="End the session a refresh token belongs to",
)
async def logout(body: RefreshRequest, session: SessionDep) -> None:
    await service.logout(session, refresh_token=body.refresh_token)


@router.get("/users/me", response_model=UserOut, tags=["users"], summary="The signed-in user")
async def me(user: CurrentUser) -> object:
    return user
