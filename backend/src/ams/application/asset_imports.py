"""Preview and idempotently confirm baseline Asset imports."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePath
from uuid import UUID, uuid4

from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    MultipartFileFingerprint,
    canonical_json_fingerprint,
    multipart_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.asset_imports import (
    AssetImport,
    AssetImportError,
    AssetImportRow,
    AssetImportStatus,
    BaselineAssetData,
)
from ams.domain.assets import Asset, Custody, CustodyAllocation
from ams.domain.authentication import UserIdentity
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole

IMPORT_PREVIEW_TTL = timedelta(hours=24)
MAX_IMPORT_FILE_SIZE_BYTES = 5 * 1024 * 1024


class AssetImportApplicationError(Exception):
    """Base class for expected baseline-import application failures."""


class UnsupportedImportFileError(AssetImportApplicationError):
    """Uploaded source is unsupported, malformed, oversized, or not template-conformant."""

    code = "UNSUPPORTED_IMPORT_FILE"


class ImportPreviewUnavailableError(AssetImportApplicationError):
    """Requested preview cannot safely be confirmed."""

    code = "IMPORT_PREVIEW_EXPIRED"


class AssetImportResultError(AssetImportApplicationError):
    """Persisted idempotency response does not match an import result contract."""

    code = "ASSET_IMPORT_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class AssetImportFile:
    """One bounded uploaded baseline file passed to a parser port."""

    filename: str
    content_type: str
    content: bytes


class IAssetImportParser(ABC):
    """Parse CSV/XLSX template bytes into framework-independent reviewed rows."""

    @abstractmethod
    def parse(self, source: AssetImportFile) -> tuple[AssetImportRow, ...]:
        """Return syntactically normalized rows or raise an unsupported-file error."""


@dataclass(frozen=True, slots=True)
class AssetImportRowResult:
    """Public row-level validation outcome."""

    row_number: int
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssetImportPreviewResult:
    """Durable preview summary returned before persistence confirmation."""

    import_id: UUID
    status: AssetImportStatus
    valid_row_count: int
    invalid_row_count: int
    rows: tuple[AssetImportRowResult, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssetImportConfirmationResult:
    """Completed import count plus rows intentionally left unpersisted."""

    import_id: UUID
    status: AssetImportStatus
    imported_asset_count: int
    rejected_rows: tuple[AssetImportRowResult, ...]
    replayed: bool


class AssetImportService:
    """Create reviewed previews and atomically import only their valid rows."""

    def __init__(
        self,
        idempotency: IIdempotentCommandExecutor,
        parser: IAssetImportParser,
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._idempotency = idempotency
        self._parser = parser
        self._clock = clock
        self._id_factory = id_factory

    async def preview(
        self,
        source: AssetImportFile,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> AssetImportPreviewResult:
        """Validate source and references, then persist a replayable reviewed snapshot."""

        self._assert_hr(actor)
        rows = self._parser.parse(source)
        source_sha256 = hashlib.sha256(source.content).hexdigest()
        fingerprint = multipart_fingerprint(
            {"filename": PurePath(source.filename).name},
            (
                MultipartFileFingerprint(
                    "file", source.content_type, source_sha256, len(source.content)
                ),
            ),
        )

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            reviewed_rows = await self._validate_references(unit_of_work, rows)
            created_at = self._clock.now()
            asset_import = AssetImport(
                id=self._id_factory(),
                created_by_user_id=actor.id,
                status=AssetImportStatus.PREVIEWED,
                source_filename=PurePath(source.filename).name,
                source_content_type=source.content_type,
                source_sha256=source_sha256,
                rows=reviewed_rows,
                expires_at=created_at + IMPORT_PREVIEW_TTL,
                created_at=created_at,
            )
            await unit_of_work.asset_imports.add(asset_import)
            return CommandResponse(200, self._preview_body(asset_import))

        response = await self._idempotency.execute(
            actor_id=actor.id,
            command_name="PREVIEW_ASSET_IMPORT",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation=operation,
        )
        return self._to_preview_result(response.body, response.replayed)

    async def confirm(
        self,
        import_id: UUID,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> AssetImportConfirmationResult:
        """Persist every reviewed-valid row once in one transaction."""

        self._assert_hr(actor)
        fingerprint = canonical_json_fingerprint({"importId": str(import_id)})

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            return await self._confirm_once(unit_of_work, import_id, actor.id)

        response = await self._idempotency.execute(
            actor_id=actor.id,
            command_name="CONFIRM_ASSET_IMPORT",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation=operation,
        )
        return self._to_confirmation_result(response.body, response.replayed)

    async def _confirm_once(
        self, unit_of_work: IUnitOfWork, import_id: UUID, actor_id: UUID
    ) -> CommandResponse:
        asset_import = await unit_of_work.asset_imports.get_for_update(import_id)
        confirmed_at = self._clock.now()
        if (
            asset_import is None
            or asset_import.created_by_user_id != actor_id
            or asset_import.status is not AssetImportStatus.PREVIEWED
            or confirmed_at >= asset_import.expires_at
        ):
            raise ImportPreviewUnavailableError("Import preview is unavailable.")
        assigner = await unit_of_work.users.get_identity(actor_id)
        if assigner is None or not assigner.is_active:
            raise ImportPreviewUnavailableError("Import preview owner is unavailable.")

        valid_rows = tuple(row for row in asset_import.rows if row.valid)
        revalidated = await self._validate_references(unit_of_work, valid_rows)
        if any(not row.valid for row in revalidated):
            raise ImportPreviewUnavailableError(
                "Import references changed after preview; upload the file again."
            )
        for row in revalidated:
            assert row.data is not None
            await self._stage_row(unit_of_work, row.data, assigner, confirmed_at)
        try:
            asset_import.complete(confirmed_at)
        except AssetImportError as exception:
            raise ImportPreviewUnavailableError("Import preview is unavailable.") from exception
        await unit_of_work.asset_imports.save(asset_import)
        rejected = tuple(row for row in asset_import.rows if not row.valid)
        return CommandResponse(
            200,
            {
                "importId": str(asset_import.id),
                "status": asset_import.status.value,
                "importedAssetCount": len(valid_rows),
                "rejectedRows": [self._row_body(row) for row in rejected],
            },
        )

    async def _stage_row(
        self,
        unit_of_work: IUnitOfWork,
        data: BaselineAssetData,
        assigner: UserIdentity,
        assigned_at: datetime,
    ) -> None:
        assignee = await unit_of_work.users.get_identity(data.custodian_user_id)
        if assignee is None or not assignee.is_active:
            raise ImportPreviewUnavailableError("Import assignee is unavailable.")
        asset_id = self._id_factory()
        custody_id = self._id_factory()
        asset = Asset.create(
            asset_id=asset_id,
            category_id=data.category_id,
            responsible_team_id=data.responsible_team_id,
            name=data.name,
            source=data.source,
            total_quantity=data.total_quantity,
            allocations=(CustodyAllocation(data.custodian_user_id, data.total_quantity),),
            created_by_user_id=assigner.id,
            created_at=assigned_at,
            description=data.description,
        )
        custody = Custody.create_pending(
            custody_id=custody_id,
            asset_id=asset_id,
            custodian_user_id=data.custodian_user_id,
            assigned_by_user_id=assigner.id,
            quantity=data.total_quantity,
            assigned_at=assigned_at,
            condition=data.condition,
            condition_note=data.condition_note,
        )
        await unit_of_work.assets.add(asset)
        await unit_of_work.custodies.add(custody)
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type="CUSTODY_ASSIGNED",
                aggregate_type="ASSET",
                aggregate_id=asset.id,
                payload={
                    "assetId": str(asset.id),
                    "custodyIds": [str(custody.id)],
                    "emails": [
                        self._assignment_email(assignee, asset.name, data.total_quantity)
                    ],
                },
                scheduled_at=assigned_at,
                created_at=assigned_at,
            )
        )
        recipients = {assigner.id: assigner, assignee.id: assignee}
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type="CUSTODY_PENDING_REMINDER",
                aggregate_type="CUSTODY",
                aggregate_id=custody.id,
                payload={
                    "assetId": str(asset.id),
                    "custodyId": str(custody.id),
                    "emails": [
                        self._reminder_email(recipient, asset.name, data.total_quantity)
                        for recipient in recipients.values()
                    ],
                },
                scheduled_at=assigned_at + timedelta(hours=24),
                created_at=assigned_at,
            )
        )

    @staticmethod
    async def _validate_references(
        unit_of_work: IUnitOfWork, rows: tuple[AssetImportRow, ...]
    ) -> tuple[AssetImportRow, ...]:
        reviewed: list[AssetImportRow] = []
        for row in rows:
            if row.data is None:
                reviewed.append(row)
                continue
            errors: list[str] = []
            if not await unit_of_work.assets.active_category_exists(row.data.category_id):
                errors.append("category_id is unavailable")
            if (
                row.data.responsible_team_id is not None
                and not await (
                    unit_of_work.assets.active_team_exists(row.data.responsible_team_id)
                )
            ):
                errors.append("responsible_team_id is unavailable")
            assignee = await unit_of_work.users.get_identity(row.data.custodian_user_id)
            if assignee is None or not assignee.is_active:
                errors.append("custodian_user_id is unavailable")
            reviewed.append(
                row if not errors else AssetImportRow(row.row_number, None, tuple(errors))
            )
        return tuple(reviewed)

    @staticmethod
    def _assert_hr(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.HR):
            raise AuthorizationError("Only an active HR actor may import Assets.")

    @staticmethod
    def _assignment_email(
        recipient: UserIdentity, asset_name: str, quantity: int
    ) -> dict[str, object]:
        return {
            "recipientId": str(recipient.id),
            "address": recipient.email,
            "subject": "Asset custody assigned",
            "body": (
                f'You were assigned {quantity} unit(s) of "{asset_name}". '
                "Please confirm receipt with evidence."
            ),
        }

    @staticmethod
    def _reminder_email(
        recipient: UserIdentity, asset_name: str, quantity: int
    ) -> dict[str, object]:
        return {
            "recipientId": str(recipient.id),
            "address": recipient.email,
            "subject": "Asset custody confirmation pending",
            "body": (
                f'Custody for {quantity} unit(s) of "{asset_name}" '
                "has been pending for 24 hours."
            ),
        }

    @classmethod
    def _preview_body(cls, asset_import: AssetImport) -> Mapping[str, object]:
        return {
            "importId": str(asset_import.id),
            "status": asset_import.status.value,
            "validRowCount": asset_import.valid_row_count,
            "invalidRowCount": asset_import.invalid_row_count,
            "rows": [cls._row_body(row) for row in asset_import.rows],
        }

    @staticmethod
    def _row_body(row: AssetImportRow) -> dict[str, object]:
        return {
            "rowNumber": row.row_number,
            "valid": row.valid,
            "errors": list(row.errors),
        }

    @staticmethod
    def _to_preview_result(
        body: Mapping[str, object], replayed: bool
    ) -> AssetImportPreviewResult:
        try:
            rows = AssetImportService._parse_result_rows(body["rows"])
            valid_count = body["validRowCount"]
            invalid_count = body["invalidRowCount"]
            status = AssetImportStatus(str(body["status"]))
            if (
                not isinstance(valid_count, int)
                or isinstance(valid_count, bool)
                or not isinstance(invalid_count, int)
                or isinstance(invalid_count, bool)
                or status is not AssetImportStatus.PREVIEWED
            ):
                raise TypeError
            return AssetImportPreviewResult(
                UUID(str(body["importId"])),
                status,
                valid_count,
                invalid_count,
                rows,
                replayed,
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise AssetImportResultError(
                "Stored import preview response is invalid."
            ) from exception

    @staticmethod
    def _to_confirmation_result(
        body: Mapping[str, object], replayed: bool
    ) -> AssetImportConfirmationResult:
        try:
            count = body["importedAssetCount"]
            status = AssetImportStatus(str(body["status"]))
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or status is not AssetImportStatus.COMPLETED
            ):
                raise TypeError
            return AssetImportConfirmationResult(
                UUID(str(body["importId"])),
                status,
                count,
                AssetImportService._parse_result_rows(body["rejectedRows"]),
                replayed,
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise AssetImportResultError(
                "Stored import confirmation response is invalid."
            ) from exception

    @staticmethod
    def _parse_result_rows(value: object) -> tuple[AssetImportRowResult, ...]:
        if not isinstance(value, list):
            raise TypeError
        results: list[AssetImportRowResult] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise TypeError
            row_number = item["rowNumber"]
            valid = item["valid"]
            errors = item["errors"]
            if (
                not isinstance(row_number, int)
                or isinstance(row_number, bool)
                or not isinstance(valid, bool)
                or not isinstance(errors, list)
                or any(not isinstance(error, str) for error in errors)
            ):
                raise TypeError
            results.append(AssetImportRowResult(row_number, valid, tuple(errors)))
        return tuple(results)
