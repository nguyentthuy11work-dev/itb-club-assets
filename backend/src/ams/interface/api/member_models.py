"""Strict HTTP models for member role administration."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field, Strict

from ams.application.member_roles import MemberPage, MemberSummary
from ams.domain.identity import OperationalRole
from ams.interface.api.models import StrictApiModel


class ReplaceRolesRequest(StrictApiModel):
    """The complete desired role set; an empty list removes every role."""

    roles: list[Annotated[OperationalRole, Strict(False)]] = Field(
        max_length=len(OperationalRole)
    )


class MemberResponse(StrictApiModel):
    """One member with effective roles."""

    id: UUID
    username: str
    email: str
    name: str
    roles: list[OperationalRole]
    is_active: bool = Field(serialization_alias="isActive")

    @classmethod
    def from_summary(cls, member: MemberSummary) -> MemberResponse:
        """Map an application member summary."""

        return cls(
            id=member.id,
            username=member.username,
            email=member.email,
            name=member.display_name,
            roles=sorted(member.roles, key=str),
            is_active=member.is_active,
        )


class MemberPageResponse(StrictApiModel):
    """Canonical paginated member list."""

    items: list[MemberResponse]
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total: int

    @classmethod
    def from_page(cls, result: MemberPage) -> MemberPageResponse:
        """Map the application page to the shared API envelope."""

        return cls(
            items=[MemberResponse.from_summary(item) for item in result.items],
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )
