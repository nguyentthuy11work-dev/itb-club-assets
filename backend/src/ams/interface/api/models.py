"""Strict API request and response models for authentication."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ams.application.authentication import LoginResult, TokenPair
from ams.domain.identity import CurrentActor, OperationalRole


class StrictApiModel(BaseModel):
    """Base model rejecting coercion and unknown request fields."""

    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)


class LoginRequest(StrictApiModel):
    """Local username/email and password login payload."""

    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(StrictApiModel):
    """Authenticated user data safe for frontend authorization presentation."""

    id: UUID
    name: str
    roles: list[OperationalRole]
    team_ids: list[UUID] = Field(serialization_alias="teamIds")


class LoginResponse(StrictApiModel):
    """Login response; refresh credentials remain in an HttpOnly cookie."""

    access_token: str = Field(serialization_alias="accessToken")
    expires_at: datetime = Field(serialization_alias="expiresAt")
    user: UserResponse

    @classmethod
    def from_result(cls, result: LoginResult) -> LoginResponse:
        """Map the application login result to the frozen API contract."""

        return cls(
            access_token=result.tokens.access_token,
            expires_at=result.tokens.access_expires_at,
            user=UserResponse(
                id=result.user.id,
                name=result.user.display_name,
                roles=sorted(result.user.roles, key=str),
                team_ids=sorted(result.user.team_ids, key=str),
            ),
        )


class RefreshResponse(StrictApiModel):
    """Rotated access-token response."""

    access_token: str = Field(serialization_alias="accessToken")
    expires_at: datetime = Field(serialization_alias="expiresAt")

    @classmethod
    def from_tokens(cls, tokens: TokenPair) -> RefreshResponse:
        """Map a rotated token pair without exposing its refresh token."""

        return cls(access_token=tokens.access_token, expires_at=tokens.access_expires_at)


class LogoutResponse(StrictApiModel):
    """Successful logout acknowledgement."""

    status: Literal["LOGGED_OUT"] = "LOGGED_OUT"


class ActorResponse(StrictApiModel):
    """Current actor response based on freshly loaded effective assignments."""

    id: UUID
    name: str
    roles: list[OperationalRole]
    team_ids: list[UUID] = Field(serialization_alias="teamIds")

    @classmethod
    def from_actor(cls, actor: CurrentActor) -> ActorResponse:
        """Map a domain actor to the public response model."""

        return cls(
            id=actor.id,
            name=actor.display_name,
            roles=sorted(actor.roles, key=str),
            team_ids=sorted(actor.team_ids, key=str),
        )


class ErrorDetail(StrictApiModel):
    """Stable machine-readable error body."""

    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(StrictApiModel):
    """Standard API error envelope."""

    error: ErrorDetail
    trace_id: UUID = Field(serialization_alias="traceId")


class HealthResponse(StrictApiModel):
    """Minimal operational endpoint response."""

    status: Literal["UP", "READY"]
