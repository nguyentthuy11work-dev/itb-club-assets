"""Thin FastAPI routes for the authentication session lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response

from ams.application.authentication import SessionService, TokenPair
from ams.domain.authentication import TokenExpiredError
from ams.domain.identity import CurrentActor
from ams.interface.api.dependencies import get_current_actor, get_session_service
from ams.interface.api.models import (
    ActorResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RefreshResponse,
)

REFRESH_COOKIE_NAME = "ams_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"


@dataclass(frozen=True, slots=True)
class AuthCookieSettings:
    """Security and lifetime settings for the refresh-token cookie."""

    secure: bool = True
    max_age_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        """Reject invalid cookie lifetimes."""

        if self.max_age_seconds <= 0:
            raise ValueError("Refresh cookie lifetime must be positive.")


router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> LoginResponse:
    """Authenticate local credentials and start a refresh-backed session."""

    result = await service.login(payload.username, payload.password)
    _set_refresh_cookie(response, result.tokens, _get_cookie_settings(request))
    return LoginResponse.from_result(result)


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_session(
    request: Request,
    response: Response,
    service: Annotated[SessionService, Depends(get_session_service)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> RefreshResponse:
    """Rotate the HttpOnly refresh cookie and return a new access token."""

    token = _require_refresh_token(refresh_token)
    tokens = await service.refresh(token)
    _set_refresh_cookie(response, tokens, _get_cookie_settings(request))
    return RefreshResponse.from_tokens(tokens)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    service: Annotated[SessionService, Depends(get_session_service)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> LogoutResponse:
    """Revoke the current refresh session and clear its browser cookie."""

    token = _require_refresh_token(refresh_token)
    await service.logout(token)
    cookie_settings = _get_cookie_settings(request)
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=cookie_settings.secure,
        httponly=True,
        samesite="strict",
    )
    return LogoutResponse()


@router.get("/me", response_model=ActorResponse)
async def get_me(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ActorResponse:
    """Return the current actor with effective roles and teams from persistence."""

    return ActorResponse.from_actor(actor)


def _require_refresh_token(refresh_token: str | None) -> str:
    if refresh_token is None:
        raise TokenExpiredError("Refresh token is required.")
    return refresh_token


def _get_cookie_settings(request: Request) -> AuthCookieSettings:
    settings: AuthCookieSettings = request.app.state.auth_cookie_settings
    return settings


def _set_refresh_cookie(
    response: Response, tokens: TokenPair, settings: AuthCookieSettings
) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=tokens.refresh_token,
        max_age=settings.max_age_seconds,
        expires=tokens.refresh_expires_at,
        path=REFRESH_COOKIE_PATH,
        secure=settings.secure,
        httponly=True,
        samesite="strict",
    )
