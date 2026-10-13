"""Idempotent reconciliation commands, snapshot reads, and notifications."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    MultipartFileFingerprint,
    canonical_json_fingerprint,
    multipart_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import AssetCondition
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.reconciliations import (
    CustodianScopeRequiredError,
    InvalidReconciliationDateError,
    InvalidReconciliationTransitionError,
    ReconciliationEvidence,
    ReconciliationForbiddenError,
    ReconciliationItem,
    ReconciliationItemStatus,
    ReconciliationNotFoundError,
    ReconciliationRoleRequiredError,
    ReconciliationSession,
    ReconciliationStatus,
    TemporaryLoanReturnTarget,
)

CLUB_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True, slots=True)
class ConfirmReconciliationMetadata:
    """Validated non-file item confirmation values."""

    actual_quantity: int
    condition: AssetCondition
    condition_note: str | None = None
    discrepancy_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """One complete session response plus replay metadata."""

    session: ReconciliationSession
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationItemResult:
    """One item command response plus replay metadata."""

    item: ReconciliationItem
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationItemPage:
    """Stable custodian reconciliation work queue."""

    items: tuple[ReconciliationItem, ...]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class ConfirmationRequestResult:
    """Confirmation request timestamp and deduplicated recipient count."""

    reconciliation_id: UUID
    requested_at: datetime
    email_recipients: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ReminderResult:
    """Pending-item reminder acceptance result."""

    item_id: UUID
    email_queued: bool
    replayed: bool = False


class ReconciliationService:
    """Orchestrate F09 through injected transaction, time, and storage ports."""

    def __init__(
        self,
        executor: IIdempotentCommandExecutor,
        unit_of_work_factory: Callable[[], IUnitOfWork],
        evidence_storage: EvidenceStorageService,
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._executor = executor
        self._unit_of_work_factory = unit_of_work_factory
        self._evidence_storage = evidence_storage
        self._clock = clock
        self._id_factory = id_factory

    async def open(
        self,
        reconciliation_date: date,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ReconciliationResult:
        """Open one globally exclusive immutable Custody snapshot."""

        self._require_hr(actor)
        now = self._clock.now()
        if reconciliation_date < now.astimezone(CLUB_TIMEZONE).date():
            raise InvalidReconciliationDateError("Date must be today or later.")

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            await unit_of_work.reconciliations.lock_open_policy()
            if await unit_of_work.reconciliations.has_open_session():
                raise InvalidReconciliationTransitionError(
                    "Another reconciliation session is already open."
                )
            candidates = await unit_of_work.reconciliations.list_snapshot_candidates()
            loan_targets = (
                await unit_of_work.reconciliations.list_temporary_loan_return_targets()
            )
            session_id = self._id_factory()
            session = ReconciliationSession(
                id=session_id,
                reconciliation_date=reconciliation_date,
                opened_by_user_id=actor.id,
                opened_at=now,
                temporary_loan_ticket_count=len({target.ticket_id for target in loan_targets}),
                items=tuple(
                    ReconciliationItem(
                        id=self._id_factory(),
                        reconciliation_id=session_id,
                        custody_id=candidate.custody_id,
                        asset_id=candidate.asset_id,
                        custodian_user_id=candidate.custodian_user_id,
                        asset_name=candidate.asset_name,
                        expected_quantity=candidate.expected_quantity,
                        expected_condition=candidate.expected_condition,
                        snapshot_at=now,
                    )
                    for candidate in candidates
                ),
            )
            await unit_of_work.reconciliations.add(session)
            await self._queue_temporary_loan_return_requests(
                unit_of_work, session, loan_targets, now
            )
            return CommandResponse(201, self._session_body(session))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="OPEN_RECONCILIATION",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(
                {"reconciliationDate": reconciliation_date.isoformat()}
            ),
            operation=operation,
        )
        return ReconciliationResult(self._session_from_body(response.body), response.replayed)

    async def get(self, reconciliation_id: UUID, actor: CurrentActor) -> ReconciliationResult:
        """Return one complete reconciliation snapshot to HR."""

        self._require_hr(actor)
        async with self._unit_of_work_factory() as unit_of_work:
            session = await unit_of_work.reconciliations.get(reconciliation_id)
        if session is None:
            raise ReconciliationNotFoundError("Reconciliation is unavailable.")
        return ReconciliationResult(session)

    async def list_mine(
        self, actor: CurrentActor, *, offset: int = 0, limit: int = 20
    ) -> ReconciliationItemPage:
        """List only items assigned to the active snapshot custodian."""

        if not actor.is_active:
            raise ReconciliationForbiddenError("Active account is required.")
        async with self._unit_of_work_factory() as unit_of_work:
            items, total = await unit_of_work.reconciliations.list_for_custodian(
                actor.id, offset=offset, limit=limit
            )
        if total == 0:
            raise CustodianScopeRequiredError("No custody scope is available.")
        return ReconciliationItemPage(items, total, offset, limit)

    async def request_confirmation(
        self, reconciliation_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ConfirmationRequestResult:
        """Queue one deduplicated email per snapshotted custodian."""

        self._require_hr(actor)

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            session = await unit_of_work.reconciliations.get_for_update(reconciliation_id)
            if session is None:
                raise ReconciliationNotFoundError("Reconciliation is unavailable.")
            requested_at = self._clock.now()
            session.request_confirmation(requested_at=requested_at)
            emails = await self._custodian_emails(unit_of_work, session)
            if emails:
                await unit_of_work.outbox.add(
                    OutboxEvent(
                        id=self._id_factory(),
                        event_type="RECONCILIATION_CONFIRMATION_REQUESTED",
                        aggregate_type="RECONCILIATION",
                        aggregate_id=session.id,
                        payload={
                            "reconciliationId": str(session.id),
                            "emails": emails,
                        },
                        scheduled_at=requested_at,
                        created_at=requested_at,
                    )
                )
            await unit_of_work.reconciliations.save_session(session, updated_at=requested_at)
            return CommandResponse(
                200,
                {
                    "id": str(session.id),
                    "requestedAt": requested_at.isoformat(),
                    "emailRecipients": len(emails),
                },
            )

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="REQUEST_RECONCILIATION_CONFIRMATION",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(
                {"reconciliationId": str(reconciliation_id)}
            ),
            operation=operation,
        )
        return ConfirmationRequestResult(
            reconciliation_id=UUID(str(response.body["id"])),
            requested_at=datetime.fromisoformat(str(response.body["requestedAt"])),
            email_recipients=int(str(response.body["emailRecipients"])),
            replayed=response.replayed,
        )

    async def confirm_item(
        self,
        item_id: UUID,
        metadata: ConfirmReconciliationMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ReconciliationItemResult:
        """Confirm one assigned item with private evidence and exact replay."""

        if not actor.is_active:
            raise ReconciliationForbiddenError("Active account is required.")
        staged_files = await self._evidence_storage.stage_images(files)
        fingerprint = multipart_fingerprint(
            {
                "itemId": str(item_id),
                "actualQuantity": metadata.actual_quantity,
                "condition": metadata.condition.value,
                "conditionNote": metadata.condition_note,
                "discrepancyReason": metadata.discrepancy_reason,
            },
            tuple(
                MultipartFileFingerprint.from_staged("files", staged) for staged in staged_files
            ),
        )
        finalized_keys: list[str] = []

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            session, item = await self._locked_item(unit_of_work, item_id)
            if session.status is not ReconciliationStatus.OPEN:
                raise InvalidReconciliationTransitionError(
                    "A closed reconciliation is immutable."
                )
            confirmed_at = self._clock.now()
            evidence_ids = tuple(self._id_factory() for _ in staged_files)
            item.confirm(
                actor_id=actor.id,
                actual_quantity=metadata.actual_quantity,
                actual_condition=metadata.condition,
                condition_note=metadata.condition_note,
                discrepancy_reason=metadata.discrepancy_reason,
                evidence_ids=evidence_ids,
                confirmed_at=confirmed_at,
            )
            object_keys = await self._evidence_storage.finalize(
                staged_files, owner_type="RECONCILIATION_ITEM", owner_id=item.id
            )
            finalized_keys.extend(object_keys)
            for evidence_id, staged, object_key in zip(
                evidence_ids, staged_files, object_keys, strict=True
            ):
                await unit_of_work.reconciliations.add_evidence(
                    ReconciliationEvidence(
                        evidence_id,
                        item.id,
                        actor.id,
                        object_key,
                        staged.content_type,
                        staged.content_sha256,
                        staged.size_bytes,
                        confirmed_at,
                    )
                )
            await unit_of_work.reconciliations.save_item(item, updated_at=confirmed_at)
            return CommandResponse(200, self._item_body(item))

        try:
            response = await self._executor.execute(
                actor_id=actor.id,
                command_name="CONFIRM_RECONCILIATION_ITEM",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                operation=operation,
            )
        except Exception:
            await self._evidence_storage.delete_objects(tuple(finalized_keys))
            await self._evidence_storage.discard(staged_files)
            raise
        if response.replayed:
            await self._evidence_storage.discard(staged_files)
        return ReconciliationItemResult(self._item_from_body(response.body), response.replayed)

    async def resolve_item(
        self,
        item_id: UUID,
        resolution_note: str,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ReconciliationItemResult:
        """Resolve one discrepancy as HR while preserving confirmer data."""

        self._require_hr(actor)

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            session, item = await self._locked_item(unit_of_work, item_id)
            if session.status is not ReconciliationStatus.OPEN:
                raise InvalidReconciliationTransitionError(
                    "A closed reconciliation is immutable."
                )
            now = self._clock.now()
            item.resolve(actor_id=actor.id, note=resolution_note, resolved_at=now)
            await unit_of_work.reconciliations.save_item(item, updated_at=now)
            return CommandResponse(200, self._item_body(item))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="RESOLVE_RECONCILIATION_ITEM",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(
                {"itemId": str(item_id), "resolutionNote": resolution_note.strip()}
            ),
            operation=operation,
        )
        return ReconciliationItemResult(self._item_from_body(response.body), response.replayed)

    async def remind_item(
        self, item_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ReminderResult:
        """Queue an HR-triggered reminder for one still-pending snapshot custodian."""

        self._require_hr(actor)

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            session, item = await self._locked_item(unit_of_work, item_id)
            if session.status is not ReconciliationStatus.OPEN:
                raise InvalidReconciliationTransitionError(
                    "A closed reconciliation is immutable."
                )
            now = self._clock.now()
            item.remind(reminded_at=now)
            recipient = await unit_of_work.users.get_identity(item.custodian_user_id)
            if recipient is None or not recipient.is_active:
                raise ReconciliationNotFoundError("Snapshot custodian is unavailable.")
            await unit_of_work.outbox.add(
                OutboxEvent(
                    id=self._id_factory(),
                    event_type="RECONCILIATION_ITEM_REMINDER",
                    aggregate_type="RECONCILIATION_ITEM",
                    aggregate_id=item.id,
                    payload={
                        "reconciliationId": str(session.id),
                        "itemId": str(item.id),
                        "emails": [
                            {
                                "recipientId": str(recipient.id),
                                "address": recipient.email,
                                "subject": "Reconciliation confirmation reminder",
                                "body": (
                                    "Please confirm your snapshot for " f'"{item.asset_name}".'
                                ),
                            }
                        ],
                    },
                    scheduled_at=now,
                    created_at=now,
                )
            )
            await unit_of_work.reconciliations.save_item(item, updated_at=now)
            return CommandResponse(202, {"id": str(item.id), "emailQueued": True})

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="REMIND_RECONCILIATION_ITEM",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint({"itemId": str(item_id)}),
            operation=operation,
        )
        return ReminderResult(
            UUID(str(response.body["id"])),
            bool(response.body["emailQueued"]),
            response.replayed,
        )

    async def close(
        self, reconciliation_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ReconciliationResult:
        """Close an open session only after a locked zero-unresolved check."""

        self._require_hr(actor)

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            session = await unit_of_work.reconciliations.get_for_update(reconciliation_id)
            if session is None:
                raise ReconciliationNotFoundError("Reconciliation is unavailable.")
            unresolved = await unit_of_work.reconciliations.count_unresolved_for_update(
                session.id
            )
            now = self._clock.now()
            session.close(actor_id=actor.id, closed_at=now, unresolved_count=unresolved)
            await unit_of_work.reconciliations.save_session(session, updated_at=now)
            return CommandResponse(200, self._session_body(session))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="CLOSE_RECONCILIATION",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(
                {"reconciliationId": str(reconciliation_id)}
            ),
            operation=operation,
        )
        return ReconciliationResult(self._session_from_body(response.body), response.replayed)

    async def _locked_item(
        self, unit_of_work: IUnitOfWork, item_id: UUID
    ) -> tuple[ReconciliationSession, ReconciliationItem]:
        session_id = await unit_of_work.reconciliations.get_item_session_id(item_id)
        if session_id is None:
            raise ReconciliationNotFoundError("Reconciliation item is unavailable.")
        session = await unit_of_work.reconciliations.get_for_update(session_id)
        item = await unit_of_work.reconciliations.get_item_for_update(item_id)
        if session is None or item is None:
            raise ReconciliationNotFoundError("Reconciliation item is unavailable.")
        return session, item

    async def _custodian_emails(
        self, unit_of_work: IUnitOfWork, session: ReconciliationSession
    ) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        for user_id in sorted({item.custodian_user_id for item in session.items}, key=str):
            user = await unit_of_work.users.get_identity(user_id)
            if user is not None and user.is_active:
                values.append(
                    {
                        "recipientId": str(user.id),
                        "address": user.email,
                        "subject": "Reconciliation confirmation requested",
                        "body": (
                            "Please inventory and confirm your assigned Asset Custodies "
                            f"for {session.reconciliation_date.isoformat()}."
                        ),
                    }
                )
        return values

    async def _queue_temporary_loan_return_requests(
        self,
        unit_of_work: IUnitOfWork,
        session: ReconciliationSession,
        targets: tuple[TemporaryLoanReturnTarget, ...],
        now: datetime,
    ) -> None:
        recipients = {target.borrower_user_id: target.borrower_email for target in targets}
        if not recipients:
            return
        emails = [
            {
                "recipientId": str(user_id),
                "address": address,
                "subject": "Return borrowed Assets before reconciliation",
                "body": (
                    "Please return all active temporary loans before "
                    f"{session.reconciliation_date.isoformat()}."
                ),
            }
            for user_id, address in sorted(recipients.items(), key=lambda value: str(value[0]))
        ]
        schedules = [("OPENED", now)]
        for days_before in (3, 1):
            local = datetime.combine(
                session.reconciliation_date,
                time(9, 0),
                tzinfo=CLUB_TIMEZONE,
            )
            scheduled_at = local.astimezone(now.tzinfo) - timedelta(days=days_before)
            if scheduled_at > now:
                schedules.append((f"D{days_before}", scheduled_at))
        for suffix, scheduled_at in schedules:
            await unit_of_work.outbox.add(
                OutboxEvent(
                    id=self._id_factory(),
                    event_type=f"RECONCILIATION_LOAN_RETURN_{suffix}",
                    aggregate_type="RECONCILIATION",
                    aggregate_id=session.id,
                    payload={
                        "reconciliationId": str(session.id),
                        "ticketIds": sorted({str(target.ticket_id) for target in targets}),
                        "emails": emails,
                    },
                    scheduled_at=scheduled_at,
                    created_at=now,
                )
            )

    @staticmethod
    def _require_hr(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.HR):
            raise ReconciliationRoleRequiredError("HR role is required.")

    @classmethod
    def _session_body(cls, session: ReconciliationSession) -> dict[str, object]:
        return {
            "id": str(session.id),
            "reconciliationDate": session.reconciliation_date.isoformat(),
            "status": session.status.value,
            "openedByUserId": str(session.opened_by_user_id),
            "openedAt": session.opened_at.isoformat(),
            "temporaryLoanTicketCount": session.temporary_loan_ticket_count,
            "confirmationRequestedAt": (
                None
                if session.confirmation_requested_at is None
                else session.confirmation_requested_at.isoformat()
            ),
            "closedByUserId": (
                None if session.closed_by_user_id is None else str(session.closed_by_user_id)
            ),
            "closedAt": None if session.closed_at is None else session.closed_at.isoformat(),
            "items": [cls._item_body(item) for item in session.items],
        }

    @staticmethod
    def _item_body(item: ReconciliationItem) -> dict[str, object]:
        return {
            "id": str(item.id),
            "reconciliationId": str(item.reconciliation_id),
            "custodyId": str(item.custody_id),
            "assetId": str(item.asset_id),
            "custodianUserId": str(item.custodian_user_id),
            "assetName": item.asset_name,
            "expectedQuantity": item.expected_quantity,
            "expectedCondition": item.expected_condition.value,
            "snapshotAt": item.snapshot_at.isoformat(),
            "status": item.status.value,
            "actualQuantity": item.actual_quantity,
            "actualCondition": (
                None if item.actual_condition is None else item.actual_condition.value
            ),
            "conditionNote": item.condition_note,
            "discrepancyReason": item.discrepancy_reason,
            "confirmedAt": (
                None if item.confirmed_at is None else item.confirmed_at.isoformat()
            ),
            "confirmedByUserId": (
                None if item.confirmed_by_user_id is None else str(item.confirmed_by_user_id)
            ),
            "resolutionNote": item.resolution_note,
            "resolvedAt": None if item.resolved_at is None else item.resolved_at.isoformat(),
            "resolvedByUserId": (
                None if item.resolved_by_user_id is None else str(item.resolved_by_user_id)
            ),
            "lastRemindedAt": (
                None if item.last_reminded_at is None else item.last_reminded_at.isoformat()
            ),
        }

    @classmethod
    def _session_from_body(cls, body: Mapping[str, object]) -> ReconciliationSession:
        raw_items = body["items"]
        assert isinstance(raw_items, list)
        return ReconciliationSession(
            id=UUID(str(body["id"])),
            reconciliation_date=date.fromisoformat(str(body["reconciliationDate"])),
            opened_by_user_id=UUID(str(body["openedByUserId"])),
            opened_at=datetime.fromisoformat(str(body["openedAt"])),
            temporary_loan_ticket_count=int(str(body["temporaryLoanTicketCount"])),
            status=ReconciliationStatus(str(body["status"])),
            confirmation_requested_at=_optional_datetime(body["confirmationRequestedAt"]),
            closed_by_user_id=_optional_uuid(body["closedByUserId"]),
            closed_at=_optional_datetime(body["closedAt"]),
            items=tuple(
                cls._item_from_body(item) for item in raw_items if isinstance(item, Mapping)
            ),
        )

    @staticmethod
    def _item_from_body(body: Mapping[str, object]) -> ReconciliationItem:
        return ReconciliationItem(
            id=UUID(str(body["id"])),
            reconciliation_id=UUID(str(body["reconciliationId"])),
            custody_id=UUID(str(body["custodyId"])),
            asset_id=UUID(str(body["assetId"])),
            custodian_user_id=UUID(str(body["custodianUserId"])),
            asset_name=str(body["assetName"]),
            expected_quantity=int(str(body["expectedQuantity"])),
            expected_condition=AssetCondition(str(body["expectedCondition"])),
            snapshot_at=datetime.fromisoformat(str(body["snapshotAt"])),
            status=ReconciliationItemStatus(str(body["status"])),
            actual_quantity=(
                None if body["actualQuantity"] is None else int(str(body["actualQuantity"]))
            ),
            actual_condition=(
                None
                if body["actualCondition"] is None
                else AssetCondition(str(body["actualCondition"]))
            ),
            condition_note=_optional_string(body["conditionNote"]),
            discrepancy_reason=_optional_string(body["discrepancyReason"]),
            confirmed_at=_optional_datetime(body["confirmedAt"]),
            confirmed_by_user_id=_optional_uuid(body["confirmedByUserId"]),
            resolution_note=_optional_string(body["resolutionNote"]),
            resolved_at=_optional_datetime(body["resolvedAt"]),
            resolved_by_user_id=_optional_uuid(body["resolvedByUserId"]),
            last_reminded_at=_optional_datetime(body["lastRemindedAt"]),
        )


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)
