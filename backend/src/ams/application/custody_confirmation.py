"""Evidence-backed, idempotent confirmation of pending Asset Custody."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ams.application.evidence import EvidenceFileInput, EvidenceStorageService
from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    MultipartFileFingerprint,
    multipart_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent, StagedFile
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import CustodyEvidence, CustodyStatus
from ams.domain.identity import AuthorizationError, CurrentActor


class CustodyConfirmationError(Exception):
    """Base class for expected confirmation application failures."""


class CustodyResourceNotFoundError(CustodyConfirmationError):
    """The requested Custody or its owning Asset does not exist."""

    code = "CUSTODY_NOT_FOUND"


class CustodyConfirmationResultError(CustodyConfirmationError):
    """Persisted replay data does not match the confirmation response contract."""

    code = "CUSTODY_CONFIRMATION_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class ConfirmCustodyMetadata:
    """Validated non-file metadata submitted with evidence images."""

    note: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmCustodyResult:
    """Public result of activating one pending Custody."""

    custody_id: UUID
    status: CustodyStatus
    confirmed_at: datetime
    replayed: bool


class CustodyConfirmationService:
    """Stage evidence and atomically activate one assignee-owned pending Custody."""

    def __init__(
        self,
        idempotency: IIdempotentCommandExecutor,
        evidence_storage: EvidenceStorageService,
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._idempotency = idempotency
        self._evidence_storage = evidence_storage
        self._clock = clock
        self._id_factory = id_factory

    async def confirm(
        self,
        *,
        custody_id: UUID,
        metadata: ConfirmCustodyMetadata,
        files: tuple[EvidenceFileInput, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ConfirmCustodyResult:
        """Confirm once, replay exact retries, and clean every unused private object."""

        if not actor.is_active:
            raise AuthorizationError("Inactive accounts cannot confirm Custody.")
        staged_files = await self._evidence_storage.stage_images(files)
        fingerprint = multipart_fingerprint(
            {"custodyId": str(custody_id), "note": metadata.note},
            tuple(
                MultipartFileFingerprint.from_staged("files", staged) for staged in staged_files
            ),
        )
        finalized_keys: list[str] = []

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            return await self._confirm_once(
                unit_of_work,
                custody_id,
                metadata,
                staged_files,
                finalized_keys,
                actor.id,
            )

        try:
            response = await self._idempotency.execute(
                actor_id=actor.id,
                command_name="CONFIRM_CUSTODY",
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
        return self._to_result(response.body, replayed=response.replayed)

    async def _confirm_once(
        self,
        unit_of_work: IUnitOfWork,
        custody_id: UUID,
        metadata: ConfirmCustodyMetadata,
        staged_files: tuple[StagedFile, ...],
        finalized_keys: list[str],
        actor_id: UUID,
    ) -> CommandResponse:
        confirmed_at = self._clock.now()
        custody = await unit_of_work.custodies.get_for_update(custody_id)
        if custody is None:
            raise CustodyResourceNotFoundError("Custody was not found.")
        asset = await unit_of_work.assets.get_for_update(custody.asset_id)
        if asset is None:
            raise CustodyResourceNotFoundError("Custody Asset was not found.")

        evidence_ids = tuple(self._id_factory() for _ in staged_files)
        custody.confirm(
            actor_id=actor_id,
            evidence_ids=evidence_ids,
            confirmed_at=confirmed_at,
        )
        asset.activate_custody(custody)
        assigner = await unit_of_work.users.get_identity(custody.assigned_by_user_id)
        if assigner is None:
            raise CustodyResourceNotFoundError("Custody assigner was not found.")

        object_keys = await self._evidence_storage.finalize(
            staged_files,
            owner_type="CUSTODY",
            owner_id=custody.id,
        )
        finalized_keys.extend(object_keys)
        for evidence_id, staged, object_key in zip(
            evidence_ids, staged_files, object_keys, strict=True
        ):
            await unit_of_work.custody_evidence.add(
                CustodyEvidence(
                    id=evidence_id,
                    custody_id=custody.id,
                    uploaded_by_user_id=actor_id,
                    object_key=object_key,
                    content_type=staged.content_type,
                    content_sha256=staged.content_sha256,
                    size_bytes=staged.size_bytes,
                    note=metadata.note,
                    created_at=confirmed_at,
                )
            )
        await unit_of_work.custodies.save(custody)
        await unit_of_work.assets.save(asset)
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type="CUSTODY_CONFIRMED",
                aggregate_type="CUSTODY",
                aggregate_id=custody.id,
                payload={
                    "assetId": str(asset.id),
                    "custodyId": str(custody.id),
                    "emails": [
                        {
                            "recipientId": str(assigner.id),
                            "address": assigner.email,
                            "subject": "Asset custody confirmed",
                            "body": (
                                f"Custody for {custody.on_hand_quantity} unit(s) of "
                                f'"{asset.name}" '
                                "was confirmed with evidence."
                            ),
                        }
                    ],
                },
                scheduled_at=confirmed_at,
                created_at=confirmed_at,
            )
        )
        return CommandResponse(
            200,
            {
                "custodyId": str(custody.id),
                "status": custody.status.value,
                "confirmedAt": confirmed_at.isoformat(),
            },
        )

    @staticmethod
    def _to_result(body: Mapping[str, object], *, replayed: bool) -> ConfirmCustodyResult:
        try:
            status = body["status"]
            if status != CustodyStatus.ACTIVE.value:
                raise ValueError
            confirmed_at = datetime.fromisoformat(str(body["confirmedAt"]))
            if confirmed_at.utcoffset() != timedelta(0):
                raise ValueError
            return ConfirmCustodyResult(
                custody_id=UUID(str(body["custodyId"])),
                status=CustodyStatus(status),
                confirmed_at=confirmed_at,
                replayed=replayed,
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise CustodyConfirmationResultError(
                "Stored Custody confirmation response is invalid."
            ) from exception
