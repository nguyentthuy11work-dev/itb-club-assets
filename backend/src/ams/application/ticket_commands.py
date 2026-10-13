"""Idempotent command orchestration for the Borrow Ticket lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    MultipartFileFingerprint,
    canonical_json_fingerprint,
    multipart_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent, StagedFile
from ams.application.ticket_reminders import TicketReminderScheduler
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import Asset, Custody, CustodyStatus
from ams.domain.identity import AuthorizationError, CurrentActor
from ams.domain.tickets import (
    BorrowTicket,
    TicketEvent,
    TicketEvidence,
    TicketMethod,
    TicketStatus,
)


class TicketCommandError(Exception):
    """Base class for expected Ticket command application failures."""


class TicketResourceNotFoundError(TicketCommandError):
    """A Ticket or one of its required aggregate rows is unavailable."""

    code = "TICKET_NOT_FOUND"


class TicketCommandResultError(TicketCommandError):
    """Stored idempotency response does not match the command contract."""

    code = "TICKET_COMMAND_RESULT_INVALID"


class EvidenceAction(StrEnum):
    """Evidence-backed Ticket actions and their persisted evidence types."""

    CONFIRM_RECEIVED = "PICKUP"
    MARK_RETURNED = "BORROWER_RETURN"
    CONFIRM_RETURNED = "CUSTODIAN_RETURN"


@dataclass(frozen=True, slots=True)
class TicketActionResult:
    """Public result shared by every Ticket transition command."""

    ticket_id: UUID
    status: TicketStatus
    replayed: bool


@dataclass(frozen=True, slots=True)
class TicketEvidenceMetadata:
    """Validated non-file metadata for one evidence-backed action."""

    note: str | None = None


class TicketLifecycleService:
    """Apply actor-aware transitions with quantity, evidence, event, and outbox effects."""

    def __init__(
        self,
        idempotency: IIdempotentCommandExecutor,
        evidence_storage: EvidenceStorageService,
        clock: IClock,
        reminder_scheduler: TicketReminderScheduler,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._idempotency = idempotency
        self._evidence_storage = evidence_storage
        self._clock = clock
        self._id_factory = id_factory
        self._reminder_scheduler = reminder_scheduler

    async def approve(
        self, ticket_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> TicketActionResult:
        """Approve one pending Ticket as its source custodian."""

        return await self._execute_json(
            ticket_id,
            actor,
            idempotency_key,
            command_name="APPROVE_BORROW_TICKET",
            fields={},
            action="APPROVED",
        )

    async def reject(
        self,
        ticket_id: UUID,
        reason: str,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TicketActionResult:
        """Reject a pending Ticket and release its reservation exactly once."""

        return await self._execute_json(
            ticket_id,
            actor,
            idempotency_key,
            command_name="REJECT_BORROW_TICKET",
            fields={"reason": reason},
            action="REJECTED",
            reason=reason,
        )

    async def request_cancellation(
        self,
        ticket_id: UUID,
        reason: str,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TicketActionResult:
        """Cancel pending directly or request resolution for an approved Ticket."""

        return await self._execute_json(
            ticket_id,
            actor,
            idempotency_key,
            command_name="REQUEST_TICKET_CANCELLATION",
            fields={"reason": reason},
            action="CANCELLATION_REQUESTED",
            reason=reason,
        )

    async def resolve_cancellation(
        self,
        ticket_id: UUID,
        approve: bool,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TicketActionResult:
        """Accept cancellation with release or restore only the approved state."""

        return await self._execute_json(
            ticket_id,
            actor,
            idempotency_key,
            command_name="RESOLVE_TICKET_CANCELLATION",
            fields={"approve": approve},
            action="CANCELLATION_RESOLVED",
            approve=approve,
        )

    async def confirm_received(
        self,
        ticket_id: UUID,
        metadata: TicketEvidenceMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TicketActionResult:
        """Confirm borrower receipt and start a loan or complete a custody transfer."""

        return await self._execute_evidence(
            ticket_id,
            metadata,
            files,
            actor,
            idempotency_key,
            EvidenceAction.CONFIRM_RECEIVED,
        )

    async def mark_returned(
        self,
        ticket_id: UUID,
        metadata: TicketEvidenceMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TicketActionResult:
        """Record borrower return evidence without releasing quantity yet."""

        return await self._execute_evidence(
            ticket_id,
            metadata,
            files,
            actor,
            idempotency_key,
            EvidenceAction.MARK_RETURNED,
        )

    async def confirm_returned(
        self,
        ticket_id: UUID,
        metadata: TicketEvidenceMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> TicketActionResult:
        """Confirm source-custodian receipt, release loan quantity, and close."""

        return await self._execute_evidence(
            ticket_id,
            metadata,
            files,
            actor,
            idempotency_key,
            EvidenceAction.CONFIRM_RETURNED,
        )

    async def _execute_json(
        self,
        ticket_id: UUID,
        actor: CurrentActor,
        idempotency_key: str,
        *,
        command_name: str,
        fields: Mapping[str, object],
        action: str,
        reason: str | None = None,
        approve: bool | None = None,
    ) -> TicketActionResult:
        self._require_active(actor)
        payload = {"ticketId": str(ticket_id), **fields}

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            return await self._apply_json_once(
                unit_of_work,
                ticket_id,
                actor.id,
                action=action,
                reason=reason,
                approve=approve,
            )

        response = await self._idempotency.execute(
            actor_id=actor.id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_json_fingerprint(payload),
            operation=operation,
        )
        return self._to_result(response.body, response.replayed)

    async def _apply_json_once(
        self,
        unit_of_work: IUnitOfWork,
        ticket_id: UUID,
        actor_id: UUID,
        *,
        action: str,
        reason: str | None,
        approve: bool | None,
    ) -> CommandResponse:
        now = self._clock.now()
        ticket = await self._ticket_for_update(unit_of_work, ticket_id)
        before = ticket.status
        release = False
        if action == "APPROVED":
            ticket.approve(actor_id)
        elif action == "REJECTED":
            assert reason is not None
            ticket.reject(actor_id, reason)
            release = True
        elif action == "CANCELLATION_REQUESTED":
            assert reason is not None
            ticket.request_cancellation(actor_id, reason)
            release = ticket.status is TicketStatus.CANCELLED
        else:
            assert approve is not None
            ticket.resolve_cancellation(actor_id, approve=approve)
            release = approve
        if release:
            await self._release_reservation(unit_of_work, ticket)
        await self._persist_transition(
            unit_of_work,
            ticket,
            actor_id,
            before,
            action,
            now,
            note=reason,
        )
        return self._response(ticket)

    async def _execute_evidence(
        self,
        ticket_id: UUID,
        metadata: TicketEvidenceMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
        action: EvidenceAction,
    ) -> TicketActionResult:
        self._require_active(actor)
        staged = await self._evidence_storage.stage_images(files)
        fingerprint = multipart_fingerprint(
            {"ticketId": str(ticket_id), "note": metadata.note, "action": action.value},
            tuple(MultipartFileFingerprint.from_staged("files", item) for item in staged),
        )
        finalized_keys: list[str] = []

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            return await self._apply_evidence_once(
                unit_of_work,
                ticket_id,
                actor.id,
                metadata,
                staged,
                finalized_keys,
                action,
            )

        try:
            response = await self._idempotency.execute(
                actor_id=actor.id,
                command_name=f"{action.name}_BORROW_TICKET",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                operation=operation,
            )
        except Exception:
            await self._evidence_storage.delete_objects(tuple(finalized_keys))
            await self._evidence_storage.discard(staged)
            raise
        if response.replayed:
            await self._evidence_storage.discard(staged)
        return self._to_result(response.body, response.replayed)

    async def _apply_evidence_once(
        self,
        unit_of_work: IUnitOfWork,
        ticket_id: UUID,
        actor_id: UUID,
        metadata: TicketEvidenceMetadata,
        staged: tuple[StagedFile, ...],
        finalized_keys: list[str],
        action: EvidenceAction,
    ) -> CommandResponse:
        now = self._clock.now()
        ticket = await self._ticket_for_update(unit_of_work, ticket_id)
        before = ticket.status
        evidence_ids = tuple(self._id_factory() for _ in staged)
        if action is EvidenceAction.CONFIRM_RECEIVED:
            ticket.confirm_received(actor_id, evidence_ids)
        elif action is EvidenceAction.MARK_RETURNED:
            ticket.mark_returned(actor_id, evidence_ids)
        else:
            ticket.confirm_returned(actor_id, evidence_ids)

        object_keys = await self._evidence_storage.finalize(
            staged, owner_type="TICKET", owner_id=ticket.id
        )
        finalized_keys.extend(object_keys)
        for evidence_id, staged_file, object_key in zip(
            evidence_ids, staged, object_keys, strict=True
        ):
            await unit_of_work.tickets.add_evidence(
                TicketEvidence(
                    id=evidence_id,
                    ticket_id=ticket.id,
                    uploaded_by_user_id=actor_id,
                    evidence_type=action.value,
                    object_key=object_key,
                    content_type=staged_file.content_type,
                    content_sha256=staged_file.content_sha256,
                    size_bytes=staged_file.size_bytes,
                    created_at=now,
                    note=metadata.note,
                )
            )

        if action is EvidenceAction.CONFIRM_RECEIVED:
            await self._receive(unit_of_work, ticket, evidence_ids, actor_id, now)
        elif action is EvidenceAction.CONFIRM_RETURNED:
            await self._return_loan(unit_of_work, ticket)
        await self._persist_transition(
            unit_of_work,
            ticket,
            actor_id,
            before,
            action.name,
            now,
            note=metadata.note,
        )
        return self._response(ticket)

    async def _receive(
        self,
        unit_of_work: IUnitOfWork,
        ticket: BorrowTicket,
        evidence_ids: tuple[UUID, ...],
        actor_id: UUID,
        occurred_at: datetime,
    ) -> None:
        custodies = await self._source_custodies(unit_of_work, ticket)
        if ticket.method is TicketMethod.TEMPORARY:
            for item in ticket.items:
                custody = custodies[item.source_custody_id]
                custody.checkout_reserved(item.quantity)
                await unit_of_work.custodies.save(custody)
            return

        assets = await self._assets_for_update(unit_of_work, ticket)
        for item in ticket.items:
            source = custodies[item.source_custody_id]
            source.transfer_out(item.quantity, transferred_at=occurred_at)
            await unit_of_work.custodies.save(source)
            target = await unit_of_work.custodies.get_current_for_update(
                item.asset_id, actor_id
            )
            if target is None:
                target = Custody.create_from_confirmed_transfer(
                    custody_id=self._id_factory(),
                    asset_id=item.asset_id,
                    custodian_user_id=actor_id,
                    assigned_by_user_id=source.custodian_user_id,
                    quantity=item.quantity,
                    confirmed_at=occurred_at,
                    evidence_ids=evidence_ids,
                    condition=source.condition,
                )
                await unit_of_work.custodies.add(target)
            else:
                if target.status is not CustodyStatus.ACTIVE:
                    raise TicketCommandError("Target Custody is not active.")
                target.receive_transfer(item.quantity)
                await unit_of_work.custodies.save(target)
            assets[item.asset_id].release_available(item.quantity)
            await unit_of_work.assets.save(assets[item.asset_id])

    async def _release_reservation(
        self, unit_of_work: IUnitOfWork, ticket: BorrowTicket
    ) -> None:
        custodies = await self._source_custodies(unit_of_work, ticket)
        assets = await self._assets_for_update(unit_of_work, ticket)
        for item in ticket.items:
            custody = custodies[item.source_custody_id]
            custody.release_reservation(item.quantity)
            assets[item.asset_id].release_available(item.quantity)
            await unit_of_work.custodies.save(custody)
        for asset in assets.values():
            await unit_of_work.assets.save(asset)

    async def _return_loan(self, unit_of_work: IUnitOfWork, ticket: BorrowTicket) -> None:
        custodies = await self._source_custodies(unit_of_work, ticket)
        assets = await self._assets_for_update(unit_of_work, ticket)
        for item in ticket.items:
            custody = custodies[item.source_custody_id]
            custody.return_temporary_loan(item.quantity)
            assets[item.asset_id].release_available(item.quantity)
            await unit_of_work.custodies.save(custody)
        for asset in assets.values():
            await unit_of_work.assets.save(asset)

    async def _source_custodies(
        self, unit_of_work: IUnitOfWork, ticket: BorrowTicket
    ) -> dict[UUID, Custody]:
        rows = await unit_of_work.custodies.get_many_for_update(
            tuple(item.source_custody_id for item in ticket.items)
        )
        result = {row.id: row for row in rows}
        if len(result) != len(ticket.items):
            raise TicketResourceNotFoundError("Ticket source Custody was not found.")
        return result

    async def _assets_for_update(
        self, unit_of_work: IUnitOfWork, ticket: BorrowTicket
    ) -> dict[UUID, Asset]:
        assets: dict[UUID, Asset] = {}
        for asset_id in sorted({item.asset_id for item in ticket.items}, key=str):
            asset = await unit_of_work.assets.get_for_update(asset_id)
            if asset is None:
                raise TicketResourceNotFoundError("Ticket Asset was not found.")
            assets[asset_id] = asset
        return assets

    async def _persist_transition(
        self,
        unit_of_work: IUnitOfWork,
        ticket: BorrowTicket,
        actor_id: UUID,
        before: TicketStatus,
        event_type: str,
        occurred_at: datetime,
        *,
        note: str | None,
    ) -> None:
        await unit_of_work.tickets.save(ticket, updated_at=occurred_at)
        await unit_of_work.tickets.add_event(
            TicketEvent(
                id=self._id_factory(),
                ticket_id=ticket.id,
                actor_user_id=actor_id,
                event_type=event_type,
                from_status=before,
                to_status=ticket.status,
                occurred_at=occurred_at,
                note=note,
            )
        )
        await self._reminder_scheduler.synchronize(unit_of_work, ticket)
        recipient_id = (
            ticket.source_custodian_user_id
            if actor_id == ticket.borrower_user_id
            else ticket.borrower_user_id
        )
        recipient = await unit_of_work.users.get_identity(recipient_id)
        if recipient is None:
            raise TicketResourceNotFoundError("Ticket participant was not found.")
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type=self._notification_event_type(event_type, ticket),
                aggregate_type="BORROW_TICKET",
                aggregate_id=ticket.id,
                payload={
                    "ticketId": str(ticket.id),
                    "reference": {"type": "TICKET", "id": str(ticket.id)},
                    "action": self._notification_action(event_type, ticket),
                    "emails": [
                        {
                            "recipientId": str(recipient.id),
                            "address": recipient.email,
                            "subject": "Borrow Ticket updated",
                            "body": f"Ticket {ticket.id} is now {ticket.status.value}.",
                        }
                    ],
                },
                scheduled_at=occurred_at,
                created_at=occurred_at,
            )
        )

    @staticmethod
    def _notification_event_type(event_type: str, ticket: BorrowTicket) -> str:
        """Map internal transition names to stable recipient-facing event names."""

        if event_type == "CANCELLATION_REQUESTED" and ticket.status is TicketStatus.CANCELLED:
            return "TICKET_CANCELLED"
        if event_type == "CANCELLATION_RESOLVED":
            return (
                "TICKET_CANCELLATION_APPROVED"
                if ticket.status is TicketStatus.CANCELLED
                else "TICKET_CANCELLATION_REJECTED"
            )
        if event_type == "MARK_RETURNED":
            return "TICKET_RETURN_AWAITING_CONFIRMATION"
        if event_type == "CONFIRM_RETURNED":
            return "TICKET_RETURN_CONFIRMED"
        if event_type == "CONFIRM_RECEIVED":
            return (
                "TICKET_CUSTODY_TRANSFER_COMPLETED"
                if ticket.method is TicketMethod.CUSTODY_TRANSFER
                else "TICKET_PICKUP_CONFIRMED"
            )
        return f"TICKET_{event_type}"

    @staticmethod
    def _notification_action(event_type: str, ticket: BorrowTicket) -> str:
        """Return a typed client action reference for an email notification."""

        if event_type == "APPROVED":
            return "CONFIRM_RECEIVED"
        if event_type == "MARK_RETURNED":
            return "CONFIRM_RETURNED"
        if (
            event_type == "CANCELLATION_REQUESTED"
            and ticket.status is TicketStatus.CANCEL_PENDING
        ):
            return "RESOLVE_CANCELLATION"
        return "VIEW_TICKET"

    @staticmethod
    async def _ticket_for_update(unit_of_work: IUnitOfWork, ticket_id: UUID) -> BorrowTicket:
        ticket = await unit_of_work.tickets.get_for_update(ticket_id)
        if ticket is None:
            raise TicketResourceNotFoundError("Ticket was not found.")
        return ticket

    @staticmethod
    def _response(ticket: BorrowTicket) -> CommandResponse:
        return CommandResponse(200, {"ticketId": str(ticket.id), "status": ticket.status.value})

    @staticmethod
    def _to_result(body: Mapping[str, object], replayed: bool) -> TicketActionResult:
        try:
            return TicketActionResult(
                ticket_id=UUID(str(body["ticketId"])),
                status=TicketStatus(str(body["status"])),
                replayed=replayed,
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise TicketCommandResultError(
                "Stored Ticket command result is invalid."
            ) from exception

    @staticmethod
    def _require_active(actor: CurrentActor) -> None:
        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot update Borrow Tickets.")
