"""Operational liveness and readiness HTTP routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ams.application.health import IReadinessProbe
from ams.interface.api.models import HealthResponse

router = APIRouter(tags=["Operations"])


def get_readiness_probe(request: Request) -> IReadinessProbe:
    """Return the readiness probe configured by the composition root."""

    probe: IReadinessProbe = request.app.state.readiness_probe
    return probe


@router.get("/health", response_model=HealthResponse, include_in_schema=False)
async def health() -> HealthResponse:
    """Report process liveness without depending on PostgreSQL."""

    return HealthResponse(status="UP")


@router.get("/ready", response_model=HealthResponse, include_in_schema=False)
async def readiness(
    probe: Annotated[IReadinessProbe, Depends(get_readiness_probe)],
) -> HealthResponse:
    """Report whether required dependencies can serve application traffic."""

    await probe.check()
    return HealthResponse(status="READY")
