"""Idempotent standalone Custody Transfer commands, queries, and notifications."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

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
from ams.domain.assets import (
    AssetCondition,
    Custody,
    CustodyEvidence,
    InsufficientLendableQuantityError,
    InvalidCustodyTransitionError,
    InvalidQuantityError,
)
from ams.domain.authentication import UserIdentity
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.transfers import (
    CustodyTransfer,
    TransferDataError,
    TransferEvidence,
    TransferEvidenceSide,
    TransferNotFoundError,
    TransferQuantityError,
    TransferRoleRequiredError,
    TransferStatus,
)


@dataclass(frozen=True, slots=True)
class CreateTransferCommand:
    """HR-selected source, target, quantity, and optional Reconciliation link."""

    source_custody_id: UUID
    target_user_id: UUID
    quantity: int
    reconciliation_item_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConfirmTransferMetadata:
    """Optional note submitted alongside confirmation evidence."""

    note: str | None = None


@dataclass(frozen=True, slots=True)
class TransferResult:
    """One Transfer command response and replay marker."""

    transfer: CustodyTransfer
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TransferPage:
    """Stable ownership-scoped Transfer page."""

    items: tuple[CustodyTransfer, ...]
    total: int
    offset: int
    limit: int


class TransferService:
    """Reserve and complete two-party Custody Transfers through injected ports."""

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

    async def create(
        self,
        command: CreateTransferCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TransferResult:
        """Reserve source quantity and create one pending two-party Transfer as HR."""

        self._require_hr(actor)

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            source = await unit_of_work.custodies.get_for_update(command.source_custody_id)
            if source is None:
                raise TransferNotFoundError("Source Custody is unavailable.")
            if source.custodian_user_id == command.target_user_id:
                raise TransferDataError("Current and target custodians must differ.")
            target_user = await unit_of_work.users.get_identity(command.target_user_id)
            if target_user is None or not target_user.is_active:
                raise TransferNotFoundError("Target custodian is unavailable.")
            asset = await unit_of_work.assets.get_for_update(source.asset_id)
            if asset is None:
                raise TransferNotFoundError("Transfer Asset is unavailable.")
            if command.reconciliation_item_id is not None:
                reference = await unit_of_work.transfers.get_reconciliation_reference(
                    command.reconciliation_item_id
                )
                if (
                    reference is None
                    or not reference.session_open
                    or reference.source_custody_id != source.id
                ):
                    raise TransferNotFoundError(
                        "Reconciliation item is unavailable for this source Custody."
                    )
            try:
                source.reserve(command.quantity)
                asset.reserve_available(command.quantity)
            except (InsufficientLendableQuantityError, InvalidQuantityError) as exception:
                raise TransferQuantityError(
                    "Requested quantity exceeds source custody."
                ) from exception
            now = self._clock.now()
            transfer = CustodyTransfer(
                id=self._id_factory(),
                source_custody_id=source.id,
                asset_id=source.asset_id,
                current_custodian_user_id=source.custodian_user_id,
                target_custodian_user_id=command.target_user_id,
                quantity=command.quantity,
                condition_snapshot=source.condition,
                created_by_user_id=actor.id,
                created_at=now,
                reconciliation_item_id=command.reconciliation_item_id,
            )
            await unit_of_work.transfers.add(transfer)
            await unit_of_work.custodies.save(source)
            await unit_of_work.assets.save(asset)
            current_user = await unit_of_work.users.get_identity(source.custodian_user_id)
            if current_user is None:
                raise TransferNotFoundError("Current custodian is unavailable.")
            await unit_of_work.outbox.add(
                OutboxEvent(
                    id=self._id_factory(),
                    event_type="CUSTODY_TRANSFER_CREATED",
                    aggregate_type="CUSTODY_TRANSFER",
                    aggregate_id=transfer.id,
                    payload={
                        "transferId": str(transfer.id),
                        "emails": self._email_payload(
                            (current_user, target_user),
                            subject="Custody Transfer confirmation requested",
                            body="Please review and confirm the Custody Transfer.",
                        ),
                    },
                    scheduled_at=now,
                    created_at=now,
                )
            )
            return CommandResponse(201, self._to_body(transfer))

        response = await self._executor.execute(
            actor_id=actor.id,
            command_name="CREATE_CUSTODY_TRANSFER",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(
                {
                    "sourceCustodyId": str(command.source_custody_id),
                    "targetUserId": str(command.target_user_id),
                    "quantity": command.quantity,
                    "reconciliationItemId": (
                        None
                        if command.reconciliation_item_id is None
                        else str(command.reconciliation_item_id)
                    ),
                }
            ),
            operation=operation,
        )
        return TransferResult(self._from_body(response.body), response.replayed)

    async def list(
        self,
        actor: CurrentActor,
        *,
        status: TransferStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> TransferPage:
        """List HR-visible or current/target-owned Transfers."""

        if not actor.is_active:
            raise TransferRoleRequiredError("Active account is required.")
        async with self._unit_of_work_factory() as unit_of_work:
            items, total = await unit_of_work.transfers.list_visible(
                actor.id,
                include_all=actor.has_role(OperationalRole.HR),
                status=status,
                offset=offset,
                limit=limit,
            )
        return TransferPage(items, total, offset, limit)

    async def confirm_current(
        self,
        transfer_id: UUID,
        metadata: ConfirmTransferMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TransferResult:
        """Confirm the current-custodian side with private evidence."""

        return await self._confirm(
            transfer_id,
            metadata,
            files,
            actor,
            idempotency_key,
            side=TransferEvidenceSide.CURRENT,
        )

    async def confirm_target(
        self,
        transfer_id: UUID,
        metadata: ConfirmTransferMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TransferResult:
        """Confirm the target-custodian side with private evidence."""

        return await self._confirm(
            transfer_id,
            metadata,
            files,
            actor,
            idempotency_key,
            side=TransferEvidenceSide.TARGET,
        )

    async def _confirm(
        self,
        transfer_id: UUID,
        metadata: ConfirmTransferMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
        *,
        side: TransferEvidenceSide,
    ) -> TransferResult:
        if not actor.is_active:
            raise TransferRoleRequiredError("Active account is required.")
        staged_files = await self._evidence_storage.stage_images(files)
        fingerprint = multipart_fingerprint(
            {
                "transferId": str(transfer_id),
                "side": side.value,
                "note": metadata.note,
            },
            tuple(
                MultipartFileFingerprint.from_staged("files", staged) for staged in staged_files
            ),
        )
        finalized_keys: list[str] = []

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            transfer = await unit_of_work.transfers.get_for_update(transfer_id)
            if transfer is None:
                raise TransferNotFoundError("Transfer is unavailable.")
            now = self._clock.now()
            evidence_ids = tuple(self._id_factory() for _ in staged_files)
            completed = (
                transfer.confirm_current(
                    actor_id=actor.id,
                    evidence_ids=evidence_ids,
                    confirmed_at=now,
                )
                if side is TransferEvidenceSide.CURRENT
                else transfer.confirm_target(
                    actor_id=actor.id,
                    evidence_ids=evidence_ids,
                    confirmed_at=now,
                )
            )
            object_keys = await self._evidence_storage.finalize(
                staged_files,
                owner_type="CUSTODY_TRANSFER",
                owner_id=transfer.id,
            )
            finalized_keys.extend(object_keys)
            for evidence_id, staged, object_key in zip(
                evidence_ids, staged_files, object_keys, strict=True
            ):
                await unit_of_work.transfers.add_evidence(
                    TransferEvidence(
                        evidence_id,
                        transfer.id,
                        side,
                        actor.id,
                        object_key,
                        staged.content_type,
                        staged.content_sha256,
                        staged.size_bytes,
                        now,
                        metadata.note,
                    )
                )
            if completed:
                await self._complete_custody(unit_of_work, transfer, now)
            await unit_of_work.transfers.save(transfer, updated_at=now)
            return CommandResponse(200, self._to_body(transfer))

        try:
            response = await self._executor.execute(
                actor_id=actor.id,
                command_name=f"CONFIRM_CUSTODY_TRANSFER_{side.value}",
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
        return TransferResult(self._from_body(response.body), response.replayed)

    async def _complete_custody(
        self,
        unit_of_work: IUnitOfWork,
        transfer: CustodyTransfer,
        completed_at: datetime,
    ) -> None:
        source, target = await unit_of_work.transfers.lock_completion_custodies(transfer)
        asset = await unit_of_work.assets.get_for_update(transfer.asset_id)
        if asset is None:
            raise TransferNotFoundError("Transfer Asset is unavailable.")
        try:
            source.transfer_out(transfer.quantity, transferred_at=completed_at)
        except (InvalidQuantityError, InvalidCustodyTransitionError) as exception:
            raise TransferQuantityError(
                "Reserved Transfer quantity is unavailable."
            ) from exception
        target_evidence = await unit_of_work.transfers.list_evidence(
            transfer.id, TransferEvidenceSide.TARGET
        )
        if not target_evidence:
            raise TransferDataError("Target confirmation evidence is unavailable.")
        custody_evidence_ids = tuple(self._id_factory() for _ in target_evidence)
        if target is None:
            target = Custody.create_from_confirmed_transfer(
                custody_id=self._id_factory(),
                asset_id=transfer.asset_id,
                custodian_user_id=transfer.target_custodian_user_id,
                assigned_by_user_id=transfer.created_by_user_id,
                quantity=transfer.quantity,
                confirmed_at=completed_at,
                evidence_ids=custody_evidence_ids,
                condition=transfer.condition_snapshot,
            )
            await unit_of_work.custodies.add(target)
        else:
            try:
                target.receive_transfer(transfer.quantity)
            except InvalidCustodyTransitionError as exception:
                raise TransferDataError(
                    "Target Custody cannot receive this Transfer."
                ) from exception
            target.evidence_ids += custody_evidence_ids
            await unit_of_work.custodies.save(target)
        # Flush a newly created target Custody before staging evidence rows that
        # reference it. Repository ports still own all persistence operations.
        await unit_of_work.custodies.save(source)
        for evidence_id, evidence in zip(custody_evidence_ids, target_evidence, strict=True):
            await unit_of_work.custody_evidence.add(
                CustodyEvidence(
                    id=evidence_id,
                    custody_id=target.id,
                    uploaded_by_user_id=evidence.uploaded_by_user_id,
                    object_key=evidence.object_key,
                    content_type=evidence.content_type,
                    content_sha256=evidence.content_sha256,
                    size_bytes=evidence.size_bytes,
                    created_at=evidence.created_at,
                    note=evidence.note,
                    evidence_type="TRANSFER_CONFIRMATION",
                )
            )
        asset.release_available(transfer.quantity)
        await unit_of_work.assets.save(asset)
        transfer.mark_custody_completed(target.id)
        recipients = []
        for user_id in (
            transfer.created_by_user_id,
            transfer.current_custodian_user_id,
            transfer.target_custodian_user_id,
        ):
            identity = await unit_of_work.users.get_identity(user_id)
            if identity is not None and identity.is_active:
                recipients.append(identity)
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type="CUSTODY_TRANSFER_COMPLETED",
                aggregate_type="CUSTODY_TRANSFER",
                aggregate_id=transfer.id,
                payload={
                    "transferId": str(transfer.id),
                    "targetCustodyId": str(target.id),
                    "emails": self._email_payload(
                        tuple(recipients),
                        subject="Custody Transfer completed",
                        body="Both parties confirmed and Custody was transferred.",
                    ),
                },
                scheduled_at=completed_at,
                created_at=completed_at,
            )
        )

    @staticmethod
    def _email_payload(
        identities: tuple[UserIdentity, ...], *, subject: str, body: str
    ) -> builtins.list[dict[str, str]]:
        values: dict[UUID, dict[str, str]] = {}
        for value in identities:
            values[value.id] = {
                "recipientId": str(value.id),
                "address": value.email,
                "subject": subject,
                "body": body,
            }
        return [values[key] for key in sorted(values, key=str)]

    @staticmethod
    def _require_hr(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.HR):
            raise TransferRoleRequiredError("HR role is required.")

    @staticmethod
    def _to_body(transfer: CustodyTransfer) -> dict[str, object]:
        return {
            "id": str(transfer.id),
            "sourceCustodyId": str(transfer.source_custody_id),
            "assetId": str(transfer.asset_id),
            "currentCustodianUserId": str(transfer.current_custodian_user_id),
            "targetCustodianUserId": str(transfer.target_custodian_user_id),
            "quantity": transfer.quantity,
            "conditionSnapshot": transfer.condition_snapshot.value,
            "createdByUserId": str(transfer.created_by_user_id),
            "createdAt": transfer.created_at.isoformat(),
            "reconciliationItemId": (
                None
                if transfer.reconciliation_item_id is None
                else str(transfer.reconciliation_item_id)
            ),
            "status": transfer.status.value,
            "currentConfirmedAt": (
                None
                if transfer.current_confirmed_at is None
                else transfer.current_confirmed_at.isoformat()
            ),
            "targetConfirmedAt": (
                None
                if transfer.target_confirmed_at is None
                else transfer.target_confirmed_at.isoformat()
            ),
            "targetCustodyId": (
                None if transfer.target_custody_id is None else str(transfer.target_custody_id)
            ),
            "completedAt": (
                None if transfer.completed_at is None else transfer.completed_at.isoformat()
            ),
        }

    @staticmethod
    def _from_body(body: Mapping[str, object]) -> CustodyTransfer:
        status = TransferStatus(str(body["status"]))
        current_at = _optional_datetime(body["currentConfirmedAt"])
        target_at = _optional_datetime(body["targetConfirmedAt"])
        return CustodyTransfer(
            id=UUID(str(body["id"])),
            source_custody_id=UUID(str(body["sourceCustodyId"])),
            asset_id=UUID(str(body["assetId"])),
            current_custodian_user_id=UUID(str(body["currentCustodianUserId"])),
            target_custodian_user_id=UUID(str(body["targetCustodianUserId"])),
            quantity=int(str(body["quantity"])),
            condition_snapshot=AssetCondition(str(body["conditionSnapshot"])),
            created_by_user_id=UUID(str(body["createdByUserId"])),
            created_at=datetime.fromisoformat(str(body["createdAt"])),
            reconciliation_item_id=_optional_uuid(body["reconciliationItemId"]),
            status=status,
            current_confirmed_by_user_id=(
                None if current_at is None else UUID(str(body["currentCustodianUserId"]))
            ),
            current_confirmed_at=current_at,
            target_confirmed_by_user_id=(
                None if target_at is None else UUID(str(body["targetCustodianUserId"]))
            ),
            target_confirmed_at=target_at,
            target_custody_id=_optional_uuid(body["targetCustodyId"]),
            completed_at=_optional_datetime(body["completedAt"]),
        )


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))
