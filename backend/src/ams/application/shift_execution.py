"""Idempotent Shift execution commands, reads, summary, and notifications."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    canonical_json_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.events import EventDayStatus, ShiftStatus
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.shift_execution import (
    EventDayExecutionSummary,
    ShiftActionForbiddenError,
    ShiftEvent,
    ShiftExecution,
    ShiftNotFoundError,
)


@dataclass(frozen=True, slots=True)
class ShiftQuantityCommand:
    """One submitted actual quantity keyed by configured Asset identity."""

    asset_id: UUID
    actual_quantity: int


@dataclass(frozen=True, slots=True)
class ShiftResult:
    """One complete Shift execution response and replay marker."""

    shift: ShiftExecution
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ShiftPage:
    """Stable assigned-Shift page."""

    items: tuple[ShiftExecution, ...]
    total: int
    offset: int
    limit: int


class ShiftExecutionService:
    """Execute and query Shifts through injected transaction and time ports."""

    def __init__(
        self,
        executor: IIdempotentCommandExecutor,
        unit_of_work_factory: Callable[[], IUnitOfWork],
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._executor = executor
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._id_factory = id_factory

    async def list(self, actor: CurrentActor, *, offset: int = 0, limit: int = 20) -> ShiftPage:
        """List only Shifts assigned to the active Shift Lead."""

        self._require_shift_lead(actor)
        async with self._unit_of_work_factory() as unit_of_work:
            items, total = await unit_of_work.shifts.list_for_assignee(
                actor.id, offset=offset, limit=limit
            )
        return ShiftPage(items, total, offset, limit)

    async def get(self, shift_id: UUID, actor: CurrentActor) -> ShiftResult:
        """Read one assigned Shift execution snapshot."""

        self._require_shift_lead(actor)
        async with self._unit_of_work_factory() as unit_of_work:
            shift = await unit_of_work.shifts.get(shift_id)
        self._require_visible(shift, actor)
        assert shift is not None
        return ShiftResult(shift)

    async def open(
        self, shift_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ShiftResult:
        """Open an assigned Shift in its schedule after its predecessor closes."""

        return await self._execute(
            shift_id,
            actor,
            idempotency_key,
            command_name="OPEN_SHIFT",
            fields={},
            transition=self._open_once,
        )

    async def confirm_received(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ShiftResult:
        """Confirm exact received quantities and enter ACTIVE."""

        return await self._execute_quantities(
            shift_id,
            quantities,
            actor,
            idempotency_key,
            command_name="CONFIRM_SHIFT_RECEIVED",
            event_type="SHIFT_RECEIVED_CONFIRMED",
        )

    async def report_opening_deviation(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        reason: str,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ShiftResult:
        """Record an explained opening mismatch and notify the Event creator."""

        return await self._execute_quantities(
            shift_id,
            quantities,
            actor,
            idempotency_key,
            command_name="REPORT_SHIFT_OPENING_DEVIATION",
            event_type="SHIFT_OPENING_DEVIATION_REPORTED",
            reason=reason,
        )

    async def close(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ShiftResult:
        """Close an active Shift and persist formula-derived consumption."""

        return await self._execute_quantities(
            shift_id,
            quantities,
            actor,
            idempotency_key,
            command_name="CLOSE_SHIFT",
            event_type="SHIFT_CLOSED",
            closing=True,
        )

    async def get_summary(
        self, event_day_id: UUID, actor: CurrentActor
    ) -> EventDayExecutionSummary:
        """Return Event progress and expose final totals only when complete."""

        if not actor.is_active or actor.roles.isdisjoint(
            {OperationalRole.TEAM_LEAD_ASSET, OperationalRole.HR}
        ):
            raise ShiftActionForbiddenError("Asset Team Lead or HR role is required.")
        async with self._unit_of_work_factory() as unit_of_work:
            summary = await unit_of_work.shifts.get_event_day_summary(event_day_id)
        if summary is None:
            raise ShiftNotFoundError("Event Day is unavailable.")
        return summary

    async def _execute_quantities(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        actor: CurrentActor,
        idempotency_key: str,
        *,
        command_name: str,
        event_type: str,
        reason: str | None = None,
        closing: bool = False,
    ) -> ShiftResult:
        values = self._quantity_map(quantities)
        fields: dict[str, object] = {
            "items": [
                {"assetId": str(asset_id), "actualQuantity": quantity}
                for asset_id, quantity in sorted(values.items(), key=lambda item: str(item[0]))
            ]
        }
        if reason is not None:
            fields["reason"] = reason.strip()

        async def transition(
            unit_of_work: IUnitOfWork, shift: ShiftExecution, before: ShiftStatus
        ) -> None:
            if closing:
                shift.close(actor_id=actor.id, quantities=values)
            elif reason is None:
                shift.confirm_received(actor_id=actor.id, quantities=values)
            else:
                shift.report_opening_deviation(
                    actor_id=actor.id, quantities=values, reason=reason
                )
            await self._persist(
                unit_of_work,
                shift,
                actor.id,
                before,
                event_type,
                fields,
            )
            if reason is not None:
                await self._notify_event_creator(unit_of_work, shift, reason)

        return await self._execute(
            shift_id,
            actor,
            idempotency_key,
            command_name=command_name,
            fields=fields,
            transition=transition,
        )

    async def _execute(
        self,
        shift_id: UUID,
        actor: CurrentActor,
        idempotency_key: str,
        *,
        command_name: str,
        fields: Mapping[str, object],
        transition: Callable[[IUnitOfWork, ShiftExecution, ShiftStatus], Awaitable[None]],
    ) -> ShiftResult:
        self._require_shift_lead(actor)
        payload = {"shiftId": str(shift_id), **fields}

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            shift = await unit_of_work.shifts.get_for_update(shift_id)
            self._require_visible(shift, actor)
            assert shift is not None
            before = shift.status
            await transition(unit_of_work, shift, before)
            return CommandResponse(200, self._to_body(shift))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(payload),
            operation=operation,
        )
        return ShiftResult(self._from_body(response.body), response.replayed)

    async def _open_once(
        self, unit_of_work: IUnitOfWork, shift: ShiftExecution, before: ShiftStatus
    ) -> None:
        predecessor = await unit_of_work.shifts.get_predecessor_for_update(
            shift.event_day_id, shift.sequence
        )
        shift.open(
            actor_id=shift.shift_lead_user_id,
            now=self._clock.now(),
            predecessor=predecessor,
        )
        await self._persist(
            unit_of_work,
            shift,
            shift.shift_lead_user_id,
            before,
            "SHIFT_OPENED",
            {},
        )

    async def _persist(
        self,
        unit_of_work: IUnitOfWork,
        shift: ShiftExecution,
        actor_id: UUID,
        before: ShiftStatus,
        event_type: str,
        metadata: Mapping[str, object],
    ) -> None:
        now = self._clock.now()
        event_status = EventDayStatus.IN_PROGRESS
        if (
            shift.status is ShiftStatus.CLOSED
            and await unit_of_work.shifts.all_other_shifts_closed(shift.event_day_id, shift.id)
        ):
            event_status = EventDayStatus.COMPLETED
        await unit_of_work.shifts.save_transition(
            shift,
            ShiftEvent(
                id=self._id_factory(),
                shift_id=shift.id,
                actor_user_id=actor_id,
                event_type=event_type,
                from_status=before,
                to_status=shift.status,
                metadata=dict(metadata),
                occurred_at=now,
            ),
            event_day_status=event_status,
        )

    async def _notify_event_creator(
        self, unit_of_work: IUnitOfWork, shift: ShiftExecution, reason: str
    ) -> None:
        recipient = await unit_of_work.users.get_identity(shift.event_day_creator_user_id)
        if recipient is None:
            raise ShiftNotFoundError("Event Day creator is unavailable.")
        now = self._clock.now()
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type="SHIFT_OPENING_DEVIATION_REPORTED",
                aggregate_type="SHIFT",
                aggregate_id=shift.id,
                payload={
                    "eventDayId": str(shift.event_day_id),
                    "shiftId": str(shift.id),
                    "reason": reason.strip(),
                    "emails": [
                        {
                            "recipientId": str(recipient.id),
                            "address": recipient.email,
                            "subject": f"Opening discrepancy: {shift.name}",
                            "body": reason.strip(),
                        }
                    ],
                },
                scheduled_at=now,
                created_at=now,
            )
        )

    @staticmethod
    def _quantity_map(commands: tuple[ShiftQuantityCommand, ...]) -> dict[UUID, int]:
        values = {command.asset_id: command.actual_quantity for command in commands}
        if len(values) != len(commands):
            from ams.domain.shift_execution import ShiftQuantityError

            raise ShiftQuantityError("Each Shift Asset may appear only once.")
        return values

    @staticmethod
    def _require_shift_lead(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.SHIFT_LEAD):
            raise ShiftActionForbiddenError("Active Shift Lead role is required.")

    @staticmethod
    def _require_visible(shift: ShiftExecution | None, actor: CurrentActor) -> None:
        if shift is None:
            raise ShiftNotFoundError("Shift is unavailable.")
        if shift.shift_lead_user_id != actor.id:
            raise ShiftActionForbiddenError(
                "Only the assigned Shift Lead may access this Shift."
            )

    @staticmethod
    def _to_body(shift: ShiftExecution) -> dict[str, object]:
        return {
            "id": str(shift.id),
            "eventDayId": str(shift.event_day_id),
            "eventDayName": shift.event_day_name,
            "eventDayCreatorUserId": str(shift.event_day_creator_user_id),
            "sequence": shift.sequence,
            "name": shift.name,
            "startsAt": shift.starts_at.isoformat(),
            "endsAt": shift.ends_at.isoformat(),
            "shiftLeadUserId": str(shift.shift_lead_user_id),
            "status": shift.status.value,
            "coreStockChanged": False,
            "items": [
                {
                    "id": str(item.id),
                    "assetId": str(item.asset_id),
                    "quotaQuantity": item.quota_quantity,
                    "carryOverQuantity": item.carry_over_quantity,
                    "expectedOpenQuantity": item.expected_open_quantity,
                    "openingActualQuantity": item.opening_actual_quantity,
                    "closingActualQuantity": item.closing_actual_quantity,
                    "consumedQuantity": item.consumed_quantity,
                    "discrepancyReason": item.discrepancy_reason,
                }
                for item in shift.items
            ],
        }

    @staticmethod
    def _from_body(body: Mapping[str, object]) -> ShiftExecution:
        from datetime import datetime

        from ams.domain.shift_execution import ShiftExecutionItem

        raw_items = body["items"]
        assert isinstance(raw_items, list)
        return ShiftExecution(
            id=UUID(str(body["id"])),
            event_day_id=UUID(str(body["eventDayId"])),
            event_day_name=str(body["eventDayName"]),
            event_day_creator_user_id=UUID(str(body["eventDayCreatorUserId"])),
            sequence=int(str(body["sequence"])),
            name=str(body["name"]),
            starts_at=datetime.fromisoformat(str(body["startsAt"])),
            ends_at=datetime.fromisoformat(str(body["endsAt"])),
            shift_lead_user_id=UUID(str(body["shiftLeadUserId"])),
            status=ShiftStatus(str(body["status"])),
            items=tuple(
                ShiftExecutionItem(
                    id=UUID(str(item["id"])),
                    asset_id=UUID(str(item["assetId"])),
                    quota_quantity=int(str(item["quotaQuantity"])),
                    carry_over_quantity=int(str(item["carryOverQuantity"])),
                    opening_actual_quantity=(
                        None
                        if item["openingActualQuantity"] is None
                        else int(str(item["openingActualQuantity"]))
                    ),
                    closing_actual_quantity=(
                        None
                        if item["closingActualQuantity"] is None
                        else int(str(item["closingActualQuantity"]))
                    ),
                    consumed_quantity=(
                        None
                        if item["consumedQuantity"] is None
                        else int(str(item["consumedQuantity"]))
                    ),
                    discrepancy_reason=(
                        None
                        if item["discrepancyReason"] is None
                        else str(item["discrepancyReason"])
                    ),
                )
                for item in raw_items
                if isinstance(item, Mapping)
            ),
        )
