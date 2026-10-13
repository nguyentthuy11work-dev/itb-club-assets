"""Strict response contracts for operational history and dashboards."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from ams.application.operational_queries import AssetTransactionPage
from ams.domain.operational_views import (
    AssetTransactionKind,
    AssetTransactionView,
    DashboardAction,
    DashboardReferenceType,
    DashboardTodoKind,
    DashboardTodoView,
    HrDashboardView,
    MemberDashboardView,
)
from ams.interface.api.models import StrictApiModel


class AssetTransactionResponse(StrictApiModel):
    """One immutable Asset history record."""

    id: str
    asset_id: UUID = Field(serialization_alias="assetId")
    asset_name: str = Field(serialization_alias="assetName")
    kind: AssetTransactionKind
    event_type: str = Field(serialization_alias="eventType")
    quantity: int
    occurred_at: datetime = Field(serialization_alias="occurredAt")
    actor_user_id: UUID = Field(serialization_alias="actorUserId")
    actor_name: str = Field(serialization_alias="actorName")
    ticket_id: UUID | None = Field(serialization_alias="ticketId")
    custody_id: UUID | None = Field(serialization_alias="custodyId")
    from_user_id: UUID | None = Field(serialization_alias="fromUserId")
    to_user_id: UUID | None = Field(serialization_alias="toUserId")

    @classmethod
    def from_view(cls, view: AssetTransactionView) -> AssetTransactionResponse:
        """Map an infrastructure-neutral transaction view."""

        return cls(**{name: getattr(view, name) for name in cls.model_fields})


class AssetTransactionPageResponse(StrictApiModel):
    """Stable offset page of Asset history."""

    items: list[AssetTransactionResponse]
    offset: int
    limit: int
    total: int

    @classmethod
    def from_page(cls, page: AssetTransactionPage) -> AssetTransactionPageResponse:
        """Map an application history page to transport data."""

        return cls(
            items=[AssetTransactionResponse.from_view(item) for item in page.items],
            offset=page.offset,
            limit=page.limit,
            total=page.total,
        )


class DashboardTodoResponse(StrictApiModel):
    """Typed work item containing only action and aggregate references."""

    id: str
    kind: DashboardTodoKind
    action: DashboardAction
    reference_type: DashboardReferenceType = Field(serialization_alias="referenceType")
    reference_id: UUID = Field(serialization_alias="referenceId")
    due_date: date | None = Field(serialization_alias="dueDate")

    @classmethod
    def from_view(cls, view: DashboardTodoView) -> DashboardTodoResponse:
        """Map one typed dashboard work item."""

        return cls(**{name: getattr(view, name) for name in cls.model_fields})


class MemberDashboardStatsResponse(StrictApiModel):
    """Actor-scoped dashboard counters."""

    active_borrow_count: int = Field(serialization_alias="activeBorrowCount")
    overdue_count: int = Field(serialization_alias="overdueCount")
    pending_approval_count: int = Field(serialization_alias="pendingApprovalCount")
    awaiting_return_confirmation_count: int = Field(
        serialization_alias="awaitingReturnConfirmationCount"
    )
    held_quantity: int = Field(serialization_alias="heldQuantity")
    pending_custody_confirmation_count: int = Field(
        serialization_alias="pendingCustodyConfirmationCount"
    )


class MemberDashboardResponse(StrictApiModel):
    """Member dashboard contract."""

    stats: MemberDashboardStatsResponse
    todos: list[DashboardTodoResponse]

    @classmethod
    def from_view(cls, view: MemberDashboardView) -> MemberDashboardResponse:
        """Map the member projection without presentation strings."""

        return cls(
            stats=MemberDashboardStatsResponse(
                **{
                    name: getattr(view.stats, name)
                    for name in MemberDashboardStatsResponse.model_fields
                }
            ),
            todos=[DashboardTodoResponse.from_view(item) for item in view.todos],
        )


class HrDashboardStatsResponse(StrictApiModel):
    """Club-wide HR dashboard counters."""

    total_asset_quantity: int = Field(serialization_alias="totalAssetQuantity")
    asset_type_count: int = Field(serialization_alias="assetTypeCount")
    temporarily_loaned_quantity: int = Field(serialization_alias="temporarilyLoanedQuantity")
    overdue_ticket_count: int = Field(serialization_alias="overdueTicketCount")
    pending_custody_confirmation_count: int = Field(
        serialization_alias="pendingCustodyConfirmationCount"
    )
    overdue_custody_confirmation_count: int = Field(
        serialization_alias="overdueCustodyConfirmationCount"
    )


class HrDashboardResponse(StrictApiModel):
    """HR dashboard contract."""

    stats: HrDashboardStatsResponse
    todos: list[DashboardTodoResponse]

    @classmethod
    def from_view(cls, view: HrDashboardView) -> HrDashboardResponse:
        """Map the HR projection without presentation strings."""

        return cls(
            stats=HrDashboardStatsResponse(
                **{
                    name: getattr(view.stats, name)
                    for name in HrDashboardStatsResponse.model_fields
                }
            ),
            todos=[DashboardTodoResponse.from_view(item) for item in view.todos],
        )
