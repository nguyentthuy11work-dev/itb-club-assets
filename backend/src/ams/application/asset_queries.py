"""Authorization-aware Asset and Custody read use cases."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ams.domain.asset_views import (
    AssetBrowseView,
    AssetDetailView,
    HeldAssetView,
    IAssetReadRepository,
)
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole


class AssetReadError(Exception):
    """Base class for expected Asset read failures."""


class AssetReadNotFoundError(AssetReadError):
    """Requested Asset is not available to the read surface."""

    code = "ASSET_NOT_FOUND"


class AssetReadPersistenceError(AssetReadError):
    """Infrastructure could not complete an Asset read safely."""

    code = "ASSET_READ_FAILED"


class TeamAssetScopeRequiredError(AssetReadError):
    """Actor has no explicit Team Head membership scope for Team Assets."""

    code = "TEAM_SCOPE_REQUIRED"


@dataclass(frozen=True, slots=True)
class HeldAssetPage:
    """Stable page of current Custodies held by one actor."""

    items: tuple[HeldAssetView, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class AssetBrowsePage:
    """Stable filtered page of Assets available to authenticated users."""

    items: tuple[AssetBrowseView, ...]
    page: int
    page_size: int
    total: int


class AssetReadService:
    """Serve authenticated read models through an injected query repository."""

    def __init__(self, repository: IAssetReadRepository) -> None:
        self._repository = repository

    async def list_held(
        self, actor: CurrentActor, *, page: int, page_size: int
    ) -> HeldAssetPage:
        """Return the actor's pending and active Custodies in a stable page."""

        self._ensure_active(actor)
        items, total = await self._repository.list_held(
            actor.id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return HeldAssetPage(items, page, page_size, total)

    async def get_detail(self, asset_id: UUID, actor: CurrentActor) -> AssetDetailView:
        """Return public Asset detail or a stable not-found failure."""

        self._ensure_active(actor)
        result = await self._repository.get_detail(asset_id)
        if result is None:
            raise AssetReadNotFoundError("Asset is not available.")
        return result

    async def list_assets(
        self,
        actor: CurrentActor,
        *,
        search: str | None,
        category_id: UUID | None,
        team_id: UUID | None,
        page: int,
        page_size: int,
    ) -> AssetBrowsePage:
        """Return a filtered Asset catalog page with eligible source Custodies."""

        self._ensure_active(actor)
        items, total = await self._repository.list_assets(
            search=search,
            category_id=category_id,
            team_id=team_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return AssetBrowsePage(items, page, page_size, total)

    async def list_my_team_assets(
        self,
        actor: CurrentActor,
        *,
        page: int,
        page_size: int,
    ) -> AssetBrowsePage:
        """Return Assets classified to the current Team Head's memberships."""

        self._ensure_active(actor)
        if not actor.has_role(OperationalRole.TEAM_HEAD) or not actor.team_ids:
            raise TeamAssetScopeRequiredError("No assigned Team Head scope is available.")
        items, total = await self._repository.list_for_teams(
            actor.team_ids,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return AssetBrowsePage(items, page, page_size, total)

    @staticmethod
    def _ensure_active(actor: CurrentActor) -> None:
        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot read Assets.")
