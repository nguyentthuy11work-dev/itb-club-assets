"""HR Asset creation with pending Custodies and transactional publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from ams.application.idempotency import (
    CommandResponse,
    IIdempotentCommandExecutor,
    canonical_json_fingerprint,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import Asset, AssetCondition, Custody, CustodyAllocation
from ams.domain.authentication import UserIdentity
from ams.domain.identity import AuthorizationError, CurrentActor, OperationalRole


class AssetCreationError(Exception):
    """Base class for expected Asset creation application failures."""


class AssetReferenceNotFoundError(AssetCreationError):
    """A category, Team, or assignee reference is absent or inactive."""

    code = "ASSET_REFERENCE_NOT_FOUND"


class AssetCreationResultError(AssetCreationError):
    """Persisted replay data does not match the application response contract."""

    code = "ASSET_CREATION_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class CreateAssetAllocationCommand:
    """One initial pending physical-custody allocation."""

    custodian_user_id: UUID
    quantity: int
    condition: AssetCondition = AssetCondition.GOOD
    condition_note: str | None = None


@dataclass(frozen=True, slots=True)
class CreateAssetCommand:
    """Framework-independent input for HR Asset registration."""

    category_id: UUID
    responsible_team_id: UUID | None
    name: str
    source: str
    total_quantity: int
    allocations: tuple[CreateAssetAllocationCommand, ...]
    description: str | None = None

    def fingerprint_payload(self) -> Mapping[str, object]:
        """Return the canonical semantic command body used for retry identity."""

        allocations = sorted(self.allocations, key=lambda item: str(item.custodian_user_id))
        return {
            "categoryId": str(self.category_id),
            "responsibleTeamId": (
                str(self.responsible_team_id) if self.responsible_team_id is not None else None
            ),
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "totalQuantity": self.total_quantity,
            "allocations": [
                {
                    "custodianUserId": str(allocation.custodian_user_id),
                    "quantity": allocation.quantity,
                    "condition": allocation.condition.value,
                    "conditionNote": allocation.condition_note,
                }
                for allocation in allocations
            ],
        }


@dataclass(frozen=True, slots=True)
class CreatedCustody:
    """Public application result for one new pending Custody."""

    id: UUID
    custodian_user_id: UUID
    quantity: int


@dataclass(frozen=True, slots=True)
class CreateAssetResult:
    """Created Asset identity and its pending Custody allocations."""

    asset_id: UUID
    custodies: tuple[CreatedCustody, ...]
    replayed: bool


class AssetCreationService:
    """Authorize and atomically create Asset, Custodies, outbox, and replay state."""

    def __init__(
        self,
        idempotency: IIdempotentCommandExecutor,
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._idempotency = idempotency
        self._clock = clock
        self._id_factory = id_factory

    async def create(
        self,
        command: CreateAssetCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> CreateAssetResult:
        """Create pending allocations once or replay the exact stored result."""

        self._assert_hr(actor)
        fingerprint = canonical_json_fingerprint(command.fingerprint_payload())

        async def operation(unit_of_work: IUnitOfWork) -> CommandResponse:
            return await self._create_once(unit_of_work, command, actor.id)

        response = await self._idempotency.execute(
            actor_id=actor.id,
            command_name="CREATE_ASSET",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            operation=operation,
        )
        return self._to_result(response.body, replayed=response.replayed)

    async def _create_once(
        self,
        unit_of_work: IUnitOfWork,
        command: CreateAssetCommand,
        actor_id: UUID,
    ) -> CommandResponse:
        assigned_at = self._clock.now()
        allocations = tuple(
            CustodyAllocation(item.custodian_user_id, item.quantity)
            for item in command.allocations
        )
        asset = Asset.create(
            asset_id=self._id_factory(),
            category_id=command.category_id,
            responsible_team_id=command.responsible_team_id,
            name=command.name,
            source=command.source,
            total_quantity=command.total_quantity,
            allocations=allocations,
            created_by_user_id=actor_id,
            created_at=assigned_at,
            description=command.description,
        )
        await self._assert_references(unit_of_work, command)
        assigner = await unit_of_work.users.get_identity(actor_id)
        if assigner is None or not assigner.is_active:
            raise AssetReferenceNotFoundError(
                "The assigning HR identity does not exist or is inactive."
            )

        custodies: list[Custody] = []
        assignees: dict[UUID, UserIdentity] = {}
        emails: list[dict[str, object]] = []
        for allocation in command.allocations:
            assignee = await unit_of_work.users.get_identity(allocation.custodian_user_id)
            if assignee is None or not assignee.is_active:
                raise AssetReferenceNotFoundError(
                    "An assigned custodian does not exist or is inactive."
                )
            custody = Custody.create_pending(
                custody_id=self._id_factory(),
                asset_id=asset.id,
                custodian_user_id=allocation.custodian_user_id,
                assigned_by_user_id=actor_id,
                quantity=allocation.quantity,
                assigned_at=assigned_at,
                condition=allocation.condition,
                condition_note=allocation.condition_note,
            )
            custodies.append(custody)
            assignees[custody.id] = assignee
            emails.append(
                {
                    "recipientId": str(assignee.id),
                    "address": assignee.email,
                    "subject": "Asset custody assigned",
                    "body": (
                        f'You were assigned {allocation.quantity} unit(s) of "{asset.name}". '
                        "Please confirm receipt with evidence."
                    ),
                }
            )

        await unit_of_work.assets.add(asset)
        for custody in custodies:
            await unit_of_work.custodies.add(custody)
        await unit_of_work.outbox.add(
            OutboxEvent(
                id=self._id_factory(),
                event_type="CUSTODY_ASSIGNED",
                aggregate_type="ASSET",
                aggregate_id=asset.id,
                payload={
                    "assetId": str(asset.id),
                    "custodyIds": [str(custody.id) for custody in custodies],
                    "emails": emails,
                },
                scheduled_at=assigned_at,
                created_at=assigned_at,
            )
        )
        for custody in custodies:
            assignee = assignees[custody.id]
            reminder_recipients = {assigner.id: assigner, assignee.id: assignee}
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
                            {
                                "recipientId": str(recipient.id),
                                "address": recipient.email,
                                "subject": "Asset custody confirmation pending",
                                "body": (
                                    f"Custody for {custody.on_hand_quantity} unit(s) of "
                                    f'"{asset.name}" has been pending for 24 hours.'
                                ),
                            }
                            for recipient in reminder_recipients.values()
                        ],
                    },
                    scheduled_at=assigned_at + timedelta(hours=24),
                    created_at=assigned_at,
                )
            )
        return CommandResponse(
            status_code=201,
            body={
                "assetId": str(asset.id),
                "custodies": [
                    {
                        "id": str(custody.id),
                        "custodianUserId": str(custody.custodian_user_id),
                        "quantity": custody.on_hand_quantity,
                    }
                    for custody in custodies
                ],
            },
        )

    @staticmethod
    async def _assert_references(
        unit_of_work: IUnitOfWork, command: CreateAssetCommand
    ) -> None:
        if not await unit_of_work.assets.active_category_exists(command.category_id):
            raise AssetReferenceNotFoundError("Asset category does not exist or is inactive.")
        if (
            command.responsible_team_id is not None
            and not await unit_of_work.assets.active_team_exists(command.responsible_team_id)
        ):
            raise AssetReferenceNotFoundError("Responsible Team does not exist or is inactive.")

    @staticmethod
    def _assert_hr(actor: CurrentActor) -> None:
        if not actor.is_active or not actor.has_role(OperationalRole.HR):
            raise AuthorizationError("Only an active HR actor may create Assets.")

    @staticmethod
    def _to_result(body: Mapping[str, object], *, replayed: bool) -> CreateAssetResult:
        try:
            asset_id = UUID(str(body["assetId"]))
            raw_custodies = body["custodies"]
            if not isinstance(raw_custodies, list):
                raise TypeError
            custodies: list[CreatedCustody] = []
            for raw in raw_custodies:
                if not isinstance(raw, Mapping):
                    raise TypeError
                quantity = raw["quantity"]
                if not isinstance(quantity, int) or isinstance(quantity, bool):
                    raise TypeError
                custodies.append(
                    CreatedCustody(
                        id=UUID(str(raw["id"])),
                        custodian_user_id=UUID(str(raw["custodianUserId"])),
                        quantity=quantity,
                    )
                )
        except (KeyError, TypeError, ValueError) as exception:
            raise AssetCreationResultError(
                "Stored Asset creation response is invalid."
            ) from exception
        return CreateAssetResult(asset_id, tuple(custodies), replayed)
