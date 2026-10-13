"""HR-only Responsible Team classification use case."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from ams.application.assets import IAssetUnitOfWork
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole


class ResponsibleTeamUpdateError(Exception):
    """Base class for expected Responsible Team update failures."""


class ResponsibleTeamAssetNotFoundError(ResponsibleTeamUpdateError):
    """Asset selected for classification does not exist."""

    code = "ASSET_NOT_FOUND"


class ResponsibleTeamNotFoundError(ResponsibleTeamUpdateError):
    """Selected Team does not exist or is inactive."""

    code = "TEAM_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class ResponsibleTeamUpdateResult:
    """Public result after assigning or clearing one Asset classification."""

    asset_id: UUID
    responsible_team_id: UUID | None


class ResponsibleTeamService:
    """Update only an Asset's nullable Responsible Team classification."""

    def __init__(self, unit_of_work_factory: Callable[[], IAssetUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def update(
        self,
        asset_id: UUID,
        responsible_team_id: UUID | None,
        actor: CurrentActor,
    ) -> ResponsibleTeamUpdateResult:
        """Assign or clear Responsible Team without changing any Custody."""

        self._assert_hr(actor)
        async with self._unit_of_work_factory() as unit_of_work:
            asset = await unit_of_work.assets.get_for_update(asset_id)
            if asset is None:
                raise ResponsibleTeamAssetNotFoundError("Asset is not available.")
            if (
                responsible_team_id is not None
                and not await unit_of_work.assets.active_team_exists(responsible_team_id)
            ):
                raise ResponsibleTeamNotFoundError("Responsible team is unavailable.")

            asset.classify_responsible_team(responsible_team_id)
            await unit_of_work.assets.save(asset)
            await unit_of_work.commit()
            return ResponsibleTeamUpdateResult(asset.id, asset.responsible_team_id)

    @staticmethod
    def _assert_hr(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.HR):
            raise AuthorizationError(
                "Only an active HR actor may update Responsible Team classification."
            )
