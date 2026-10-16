"""Thin FastAPI routes for member role administration."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ams.application.member_roles import MemberRoleService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.dependencies import RequireRoles, get_member_role_service
from ams.interface.api.member_models import (
    MemberPageResponse,
    MemberResponse,
    ReplaceRolesRequest,
)

ROLE_ADMIN_REQUIRED = RequireRoles(OperationalRole.SYSTEM_ADMIN, OperationalRole.HR)

router = APIRouter(prefix="/hr/members", tags=["Members"])


@router.get("", response_model=MemberPageResponse)
async def list_members(
    actor: Annotated[CurrentActor, Depends(ROLE_ADMIN_REQUIRED)],
    service: Annotated[MemberRoleService, Depends(get_member_role_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MemberPageResponse:
    """List members and their effective roles."""

    result = await service.list_members(actor, page=page, page_size=page_size)
    return MemberPageResponse.from_page(result)


@router.put("/{member_id}/roles", response_model=MemberResponse)
async def replace_member_roles(
    member_id: UUID,
    payload: ReplaceRolesRequest,
    actor: Annotated[CurrentActor, Depends(ROLE_ADMIN_REQUIRED)],
    service: Annotated[MemberRoleService, Depends(get_member_role_service)],
) -> MemberResponse:
    """Replace a member's role set; elevated roles require SYSTEM_ADMIN."""

    member = await service.replace_roles(actor, member_id, frozenset(payload.roles))
    return MemberResponse.from_summary(member)
