"""Thin FastAPI routes for HR Asset administration."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status

from ams.application.asset_creation import AssetCreationService
from ams.application.responsible_team import ResponsibleTeamService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.asset_models import (
    CreateAssetRequest,
    CreateAssetResponse,
    ResponsibleTeamUpdateRequest,
    ResponsibleTeamUpdateResponse,
)
from ams.interface.api.dependencies import (
    RequireRoles,
    get_asset_creation_service,
    get_responsible_team_service,
)

HR_REQUIRED = RequireRoles(OperationalRole.HR)

router = APIRouter(prefix="/hr/assets", tags=["Assets"])


@router.post("", response_model=CreateAssetResponse, status_code=status.HTTP_201_CREATED)
async def create_asset(
    payload: CreateAssetRequest,
    actor: Annotated[CurrentActor, Depends(HR_REQUIRED)],
    service: Annotated[AssetCreationService, Depends(get_asset_creation_service)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
) -> CreateAssetResponse:
    """Create one Asset and pending Custodies through the idempotent HR use case."""

    result = await service.create(payload.to_command(), actor, idempotency_key)
    return CreateAssetResponse.from_result(result)


@router.patch("/{asset_id}/responsible-team", response_model=ResponsibleTeamUpdateResponse)
async def update_responsible_team(
    asset_id: UUID,
    payload: ResponsibleTeamUpdateRequest,
    actor: Annotated[CurrentActor, Depends(HR_REQUIRED)],
    service: Annotated[ResponsibleTeamService, Depends(get_responsible_team_service)],
) -> ResponsibleTeamUpdateResponse:
    """Assign or clear an Asset's Responsible Team classification as HR."""

    result = await service.update(asset_id, payload.responsible_team_id, actor)
    return ResponsibleTeamUpdateResponse.from_result(result)
