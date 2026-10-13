"""Idempotent term Handover snapshot, confirmation, and bounded CSV export."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    canonical_json_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.authentication import UserIdentity
from ams.domain.handovers import (
    Handover,
    HandoverAlreadyExistsError,
    HandoverCustodySnapshot,
    HandoverExportForbiddenError,
    HandoverExportLimitError,
    HandoverItem,
    HandoverNotFoundError,
    HandoverRoleRequiredError,
    HandoverStatus,
    TermNotEndedError,
    TermNotFoundError,
)
from ams.domain.identity import CurrentActor, OperationalRole

CLUB_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_EXPORT_ITEMS = 10_000
MAX_EXPORT_BYTES = 5 * 1024 * 1024
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _neutralize_csv_formula(value: str) -> str:
    if value.startswith(CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value


@dataclass(frozen=True, slots=True)
class HandoverResult:
    """One Handover command/read response and replay marker."""

    handover: Handover
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class HandoverCsvExport:
    """Authorized bounded synchronous CSV document."""

    filename: str
    content: bytes
    media_type: str = "text/csv; charset=utf-8"


class HandoverService:
    """Create immutable term snapshots and coordinate independent HR confirmations."""

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

    async def create(
        self, term_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> HandoverResult:
        """Create the term's single Asset/Custody snapshot as its old HR actor."""

        self._require_hr(actor)

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            term = await unit_of_work.handovers.get_term(term_id)
            if term is None:
                raise TermNotFoundError("Term is unavailable.")
            if actor.id != term.old_hr_user_id:
                raise HandoverRoleRequiredError(
                    "Only the configured old-side HR may create this Handover."
                )
            now = self._clock.now()
            if now.astimezone(CLUB_TIMEZONE).date() < term.ends_on:
                raise TermNotEndedError("Term has not reached its end date.")
            if await unit_of_work.handovers.has_handover_for_term(term.id):
                raise HandoverAlreadyExistsError("A Handover already exists for this term.")
            old_user = await unit_of_work.users.get_identity(term.old_hr_user_id)
            new_user = await unit_of_work.users.get_identity(term.new_hr_user_id)
            if (
                old_user is None
                or not old_user.is_active
                or new_user is None
                or not new_user.is_active
            ):
                raise TermNotFoundError("Configured Handover actors are unavailable.")
            projections = await unit_of_work.handovers.snapshot_assets(term)
            handover_id = self._id_factory()
            items = tuple(
                HandoverItem(
                    id=self._id_factory(),
                    handover_id=handover_id,
                    asset_id=value.asset_id,
                    asset_name=value.asset_name,
                    total_quantity=value.total_quantity,
                    available_quantity=value.available_quantity,
                    condition=value.condition,
                    custodies=value.custodies,
                    is_stale=term.is_stale(
                        self._local_date(value.last_qualifying_transaction_at)
                    ),
                    last_qualifying_transaction_at=value.last_qualifying_transaction_at,
                    snapshot_at=now,
                )
                for value in projections
            )
            handover = Handover(
                id=handover_id,
                term_id=term.id,
                term_code=term.code,
                term_name=term.name,
                term_starts_on=term.starts_on,
                term_ends_on=term.ends_on,
                old_hr_user_id=term.old_hr_user_id,
                new_hr_user_id=term.new_hr_user_id,
                created_by_user_id=actor.id,
                created_at=now,
                items=items,
            )
            await unit_of_work.handovers.add(handover)
            await unit_of_work.outbox.add(
                self._event(
                    handover,
                    "HANDOVER_CREATED",
                    now,
                    (old_user, new_user),
                    "Term Handover confirmation requested",
                    "Review the term snapshot and confirm your Handover side.",
                )
            )
            return CommandResponse(201, self._to_body(handover))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="CREATE_HANDOVER",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint({"termId": str(term_id)}),
            operation=operation,
        )
        return HandoverResult(self._from_body(response.body), response.replayed)

    async def get(self, handover_id: UUID, actor: CurrentActor) -> HandoverResult:
        """Return a complete snapshot to either configured HR actor."""

        self._require_hr(actor)
        async with self._unit_of_work_factory() as unit_of_work:
            handover = await unit_of_work.handovers.get(handover_id)
            self._require_participant(handover, actor)
            assert handover is not None
            return HandoverResult(handover)

    async def confirm_old(
        self, handover_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> HandoverResult:
        """Confirm the old-term side and lock when the new side already confirmed."""

        return await self._confirm(handover_id, actor, idempotency_key, old_side=True)

    async def confirm_new(
        self, handover_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> HandoverResult:
        """Confirm the new-term side and lock when the old side already confirmed."""

        return await self._confirm(handover_id, actor, idempotency_key, old_side=False)

    async def export_csv(self, handover_id: UUID, actor: CurrentActor) -> HandoverCsvExport:
        """Build an actor-authorized CSV under the approved item and byte limits."""

        self._require_hr(actor)
        async with self._unit_of_work_factory() as unit_of_work:
            handover = await unit_of_work.handovers.get(handover_id)
        if handover is None:
            raise HandoverNotFoundError("Handover is unavailable.")
        if actor.id not in {handover.old_hr_user_id, handover.new_hr_user_id}:
            raise HandoverExportForbiddenError("You cannot export this Handover.")
        if len(handover.items) > MAX_EXPORT_ITEMS:
            raise HandoverExportLimitError("Handover exceeds the CSV item limit.")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            (
                "asset_id",
                "asset_name",
                "total_quantity",
                "available_quantity",
                "condition",
                "is_stale",
                "last_qualifying_transaction_at",
                "custodies",
            )
        )
        for item in handover.items:
            writer.writerow(
                (
                    item.asset_id,
                    _neutralize_csv_formula(item.asset_name),
                    item.total_quantity,
                    item.available_quantity,
                    item.condition.value,
                    str(item.is_stale).lower(),
                    (
                        item.last_qualifying_transaction_at.isoformat()
                        if item.last_qualifying_transaction_at
                        else ""
                    ),
                    json.dumps(
                        [self._custody_to_body(value) for value in item.custodies],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
        content = ("\ufeff" + stream.getvalue()).encode("utf-8")
        if len(content) > MAX_EXPORT_BYTES:
            raise HandoverExportLimitError("Handover exceeds the CSV byte limit.")
        return HandoverCsvExport(
            filename=f"handover-{handover.id}.csv",
            content=content,
        )

    async def _confirm(
        self,
        handover_id: UUID,
        actor: CurrentActor,
        idempotency_key: str,
        *,
        old_side: bool,
    ) -> HandoverResult:
        self._require_hr(actor)
        side = "OLD" if old_side else "NEW"

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            handover = await unit_of_work.handovers.get_for_update(handover_id)
            if handover is None:
                raise HandoverNotFoundError("Handover is unavailable.")
            now = self._clock.now()
            locked = (
                handover.confirm_old(actor_id=actor.id, confirmed_at=now)
                if old_side
                else handover.confirm_new(actor_id=actor.id, confirmed_at=now)
            )
            await unit_of_work.handovers.save(handover, updated_at=now)
            recipients = await self._participants(unit_of_work, handover)
            event_type = "HANDOVER_LOCKED" if locked else f"HANDOVER_{side}_CONFIRMED"
            await unit_of_work.outbox.add(
                self._event(
                    handover,
                    event_type,
                    now,
                    recipients,
                    "Term Handover locked" if locked else "Handover side confirmed",
                    (
                        "Both sides confirmed the immutable term record."
                        if locked
                        else f"The {side.lower()} Handover side has confirmed."
                    ),
                )
            )
            return CommandResponse(200, self._to_body(handover))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name=f"CONFIRM_HANDOVER_{side}",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(
                {"handoverId": str(handover_id), "side": side}
            ),
            operation=operation,
        )
        return HandoverResult(self._from_body(response.body), response.replayed)

    async def _participants(
        self, unit_of_work: IUnitOfWork, handover: Handover
    ) -> tuple[UserIdentity, ...]:
        values = []
        for user_id in (handover.old_hr_user_id, handover.new_hr_user_id):
            identity = await unit_of_work.users.get_identity(user_id)
            if identity is not None and identity.is_active:
                values.append(identity)
        return tuple(values)

    def _event(
        self,
        handover: Handover,
        event_type: str,
        now: datetime,
        recipients: tuple[UserIdentity, ...],
        subject: str,
        body: str,
    ) -> OutboxEvent:
        return OutboxEvent(
            id=self._id_factory(),
            event_type=event_type,
            aggregate_type="HANDOVER",
            aggregate_id=handover.id,
            payload={
                "handoverId": str(handover.id),
                "emails": [
                    {
                        "recipientId": str(value.id),
                        "address": value.email,
                        "subject": subject,
                        "body": body,
                    }
                    for value in recipients
                ],
            },
            scheduled_at=now,
            created_at=now,
        )

    @staticmethod
    def _require_hr(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.HR):
            raise HandoverRoleRequiredError("HR role is required.")

    @staticmethod
    def _require_participant(handover: Handover | None, actor: CurrentActor) -> None:
        if handover is None or actor.id not in {
            handover.old_hr_user_id,
            handover.new_hr_user_id,
        }:
            raise HandoverNotFoundError("Handover is unavailable.")

    @staticmethod
    def _local_date(value: datetime | None) -> date | None:
        return None if value is None else value.astimezone(CLUB_TIMEZONE).date()

    @classmethod
    def _to_body(cls, handover: Handover) -> dict[str, object]:
        return {
            "id": str(handover.id),
            "termId": str(handover.term_id),
            "termCode": handover.term_code,
            "termName": handover.term_name,
            "termStartsOn": handover.term_starts_on.isoformat(),
            "termEndsOn": handover.term_ends_on.isoformat(),
            "oldHrUserId": str(handover.old_hr_user_id),
            "newHrUserId": str(handover.new_hr_user_id),
            "createdByUserId": str(handover.created_by_user_id),
            "createdAt": handover.created_at.isoformat(),
            "status": handover.status.value,
            "oldConfirmedAt": cls._optional_datetime_body(handover.old_confirmed_at),
            "newConfirmedAt": cls._optional_datetime_body(handover.new_confirmed_at),
            "lockedAt": cls._optional_datetime_body(handover.locked_at),
            "items": [
                {
                    "id": str(item.id),
                    "handoverId": str(item.handover_id),
                    "assetId": str(item.asset_id),
                    "assetName": item.asset_name,
                    "totalQuantity": item.total_quantity,
                    "availableQuantity": item.available_quantity,
                    "condition": item.condition.value,
                    "custodies": [cls._custody_to_body(value) for value in item.custodies],
                    "isStale": item.is_stale,
                    "lastQualifyingTransactionAt": cls._optional_datetime_body(
                        item.last_qualifying_transaction_at
                    ),
                    "snapshotAt": item.snapshot_at.isoformat(),
                }
                for item in handover.items
            ],
        }

    @classmethod
    def _from_body(cls, body: Mapping[str, object]) -> Handover:
        handover_id = UUID(str(body["id"]))
        raw_items = body["items"]
        assert isinstance(raw_items, list)
        items = tuple(cls._item_from_body(handover_id, value) for value in raw_items)
        old_at = cls._optional_datetime(body["oldConfirmedAt"])
        new_at = cls._optional_datetime(body["newConfirmedAt"])
        return Handover(
            id=handover_id,
            term_id=UUID(str(body["termId"])),
            term_code=str(body["termCode"]),
            term_name=str(body["termName"]),
            term_starts_on=date.fromisoformat(str(body["termStartsOn"])),
            term_ends_on=date.fromisoformat(str(body["termEndsOn"])),
            old_hr_user_id=UUID(str(body["oldHrUserId"])),
            new_hr_user_id=UUID(str(body["newHrUserId"])),
            created_by_user_id=UUID(str(body["createdByUserId"])),
            created_at=datetime.fromisoformat(str(body["createdAt"])),
            items=items,
            status=HandoverStatus(str(body["status"])),
            old_confirmed_by_user_id=(
                None if old_at is None else UUID(str(body["oldHrUserId"]))
            ),
            old_confirmed_at=old_at,
            new_confirmed_by_user_id=(
                None if new_at is None else UUID(str(body["newHrUserId"]))
            ),
            new_confirmed_at=new_at,
            locked_at=cls._optional_datetime(body["lockedAt"]),
        )

    @classmethod
    def _item_from_body(cls, handover_id: UUID, value: object) -> HandoverItem:
        assert isinstance(value, Mapping)
        raw_custodies = value["custodies"]
        assert isinstance(raw_custodies, list)
        return HandoverItem(
            id=UUID(str(value["id"])),
            handover_id=handover_id,
            asset_id=UUID(str(value["assetId"])),
            asset_name=str(value["assetName"]),
            total_quantity=int(str(value["totalQuantity"])),
            available_quantity=int(str(value["availableQuantity"])),
            condition=AssetCondition(str(value["condition"])),
            custodies=tuple(cls._custody_from_body(item) for item in raw_custodies),
            is_stale=bool(value["isStale"]),
            last_qualifying_transaction_at=cls._optional_datetime(
                value["lastQualifyingTransactionAt"]
            ),
            snapshot_at=datetime.fromisoformat(str(value["snapshotAt"])),
        )

    @staticmethod
    def _custody_to_body(value: HandoverCustodySnapshot) -> dict[str, object]:
        return {
            "custodyId": str(value.custody_id),
            "custodianUserId": str(value.custodian_user_id),
            "status": value.status.value,
            "onHandQuantity": value.on_hand_quantity,
            "reservedQuantity": value.reserved_quantity,
            "temporarilyLoanedQuantity": value.temporarily_loaned_quantity,
            "condition": value.condition.value,
            "conditionNote": value.condition_note,
        }

    @staticmethod
    def _custody_from_body(value: object) -> HandoverCustodySnapshot:
        assert isinstance(value, Mapping)
        return HandoverCustodySnapshot(
            custody_id=UUID(str(value["custodyId"])),
            custodian_user_id=UUID(str(value["custodianUserId"])),
            status=CustodyStatus(str(value["status"])),
            on_hand_quantity=int(str(value["onHandQuantity"])),
            reserved_quantity=int(str(value["reservedQuantity"])),
            temporarily_loaned_quantity=int(str(value["temporarilyLoanedQuantity"])),
            condition=AssetCondition(str(value["condition"])),
            condition_note=(
                None if value["conditionNote"] is None else str(value["conditionNote"])
            ),
        )

    @staticmethod
    def _optional_datetime_body(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    @staticmethod
    def _optional_datetime(value: object) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))
