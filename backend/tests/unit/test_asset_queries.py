"""Unit tests for authenticated Asset and Custody read use cases."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from ams.application.asset_queries import (
    AssetReadNotFoundError,
    AssetReadService,
    TeamAssetScopeRequiredError,
)
from ams.domain.asset_views import (
    AssetBrowseView,
    AssetCustodyView,
    AssetDetailView,
    EligibleCustodyView,
    HeldAssetView,
    IAssetReadRepository,
)
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole

ACTOR_ID = UUID("00000000-0000-4000-8000-000000002001")
ASSET_ID = UUID("00000000-0000-4000-8000-000000002002")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002003")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002004")
TEAM_ID = UUID("00000000-0000-4000-8000-000000002005")
NOW = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)


class FakeAssetReadRepository(IAssetReadRepository):
    """Return configured projections while recording pagination inputs."""

    def __init__(self, detail: AssetDetailView | None = None) -> None:
        self.held = (_held(),)
        self.detail = detail
        self.list_call: tuple[UUID, int, int] | None = None
        self.browse_call: tuple[str | None, UUID | None, UUID | None, int, int] | None = None
        self.team_call: tuple[frozenset[UUID], int, int] | None = None

    async def list_held(
        self, actor_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[HeldAssetView, ...], int]:
        """Return one configured held-Custody page."""

        self.list_call = actor_id, offset, limit
        return self.held, 3

    async def get_detail(self, asset_id: UUID) -> AssetDetailView | None:
        """Return the configured detail projection."""

        return self.detail

    async def list_assets(
        self,
        *,
        search: str | None,
        category_id: UUID | None,
        team_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        """Return one browse row and record every normalized query input."""

        self.browse_call = search, category_id, team_id, offset, limit
        return (_browse(),), 4

    async def list_for_teams(
        self,
        team_ids: frozenset[UUID],
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        """Return one Team-scoped row while recording the trusted scope."""

        self.team_call = team_ids, offset, limit
        return (_browse(),), 2


class AssetReadServiceTests(unittest.IsolatedAsyncioTestCase):
    """Verify actor scope, pagination, and stable missing-Asset behavior."""

    async def test_list_held_scopes_actor_and_calculates_offset(self) -> None:
        """The service passes only actor identity and normalized page bounds to storage."""

        repository = FakeAssetReadRepository()
        result = await AssetReadService(repository).list_held(_actor(), page=2, page_size=20)

        self.assertEqual(repository.list_call, (ACTOR_ID, 20, 20))
        self.assertEqual(result.items, repository.held)
        self.assertEqual((result.page, result.page_size, result.total), (2, 20, 3))

    async def test_get_detail_returns_projection_or_not_found(self) -> None:
        """An existing Asset is returned and an absent one uses the stable query error."""

        detail = _detail()
        self.assertIs(
            await AssetReadService(FakeAssetReadRepository(detail)).get_detail(
                ASSET_ID, _actor()
            ),
            detail,
        )
        with self.assertRaises(AssetReadNotFoundError):
            await AssetReadService(FakeAssetReadRepository()).get_detail(ASSET_ID, _actor())

    async def test_list_assets_passes_filters_and_calculates_offset(self) -> None:
        """The browse use case preserves typed filters and shared pagination semantics."""

        repository = FakeAssetReadRepository()
        result = await AssetReadService(repository).list_assets(
            _actor(),
            search="Camera",
            category_id=CATEGORY_ID,
            team_id=TEAM_ID,
            page=3,
            page_size=10,
        )

        self.assertEqual(
            repository.browse_call,
            ("Camera", CATEGORY_ID, TEAM_ID, 20, 10),
        )
        self.assertEqual(result.items, (_browse(),))
        self.assertEqual((result.page, result.page_size, result.total), (3, 10, 4))

    async def test_team_head_query_uses_only_server_derived_team_scope(self) -> None:
        """Team Asset reads pass current memberships rather than caller-supplied IDs."""

        repository = FakeAssetReadRepository()
        actor = CurrentActor(
            ACTOR_ID,
            frozenset({OperationalRole.TEAM_HEAD}),
            team_ids=frozenset({TEAM_ID}),
            is_active=True,
        )

        result = await AssetReadService(repository).list_my_team_assets(
            actor, page=2, page_size=10
        )

        self.assertEqual(repository.team_call, (frozenset({TEAM_ID}), 10, 10))
        self.assertEqual(result.items, (_browse(),))
        self.assertEqual((result.page, result.page_size, result.total), (2, 10, 2))

    async def test_team_query_requires_explicit_role_and_membership(self) -> None:
        """HR alone and Team Heads without memberships receive no implicit scope."""

        service = AssetReadService(FakeAssetReadRepository())
        actors = (
            CurrentActor(
                ACTOR_ID,
                frozenset({OperationalRole.HR}),
                team_ids=frozenset({TEAM_ID}),
                is_active=True,
            ),
            CurrentActor(
                ACTOR_ID,
                frozenset({OperationalRole.TEAM_HEAD}),
                team_ids=frozenset(),
                is_active=True,
            ),
        )
        for actor in actors:
            with self.subTest(actor=actor), self.assertRaises(TeamAssetScopeRequiredError):
                await service.list_my_team_assets(actor, page=1, page_size=20)

    async def test_inactive_actor_cannot_use_either_query(self) -> None:
        """Inactive identity state is denied before repository access."""

        service = AssetReadService(FakeAssetReadRepository(_detail()))
        inactive = CurrentActor(ACTOR_ID, frozenset(), is_active=False)
        with self.assertRaises(AuthorizationError):
            await service.list_held(inactive, page=1, page_size=20)
        with self.assertRaises(AuthorizationError):
            await service.get_detail(ASSET_ID, inactive)
        with self.assertRaises(AuthorizationError):
            await service.list_assets(
                inactive,
                search=None,
                category_id=None,
                team_id=None,
                page=1,
                page_size=20,
            )
        with self.assertRaises(AuthorizationError):
            await service.list_my_team_assets(inactive, page=1, page_size=20)


def _actor() -> CurrentActor:
    return CurrentActor(ACTOR_ID, frozenset(), is_active=True)


def _held() -> HeldAssetView:
    return HeldAssetView(
        custody_id=CUSTODY_ID,
        asset_id=ASSET_ID,
        asset_name="Camera",
        status=CustodyStatus.ACTIVE,
        on_hand_quantity=3,
        reserved_quantity=1,
        temporarily_loaned_quantity=0,
        lendable_quantity=2,
        condition=AssetCondition.GOOD,
        assigned_at=NOW,
        confirmed_at=NOW,
    )


def _detail() -> AssetDetailView:
    custody = AssetCustodyView(
        custody_id=CUSTODY_ID,
        custodian_user_id=ACTOR_ID,
        status=CustodyStatus.ACTIVE,
        on_hand_quantity=3,
        reserved_quantity=1,
        temporarily_loaned_quantity=0,
        lendable_quantity=2,
        condition=AssetCondition.GOOD,
        condition_note=None,
        assigned_at=NOW,
        confirmed_at=NOW,
        evidence_count=1,
    )
    return AssetDetailView(
        id=ASSET_ID,
        category_id=CATEGORY_ID,
        responsible_team_id=None,
        name="Camera",
        description=None,
        source="DONATION",
        total_quantity=3,
        available_quantity=2,
        custodies=(custody,),
    )


def _browse() -> AssetBrowseView:
    return AssetBrowseView(
        id=ASSET_ID,
        category_id=CATEGORY_ID,
        responsible_team_id=TEAM_ID,
        name="Camera",
        description=None,
        source="DONATION",
        total_quantity=3,
        available_quantity=2,
        eligible_custodies=(EligibleCustodyView(CUSTODY_ID, 2),),
    )
