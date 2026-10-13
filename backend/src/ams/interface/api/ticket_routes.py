"""Thin JSON and multipart routes for the Borrow Ticket lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from ams.application.evidence import EvidenceFileInput
from ams.application.ticket_commands import (
    TicketActionResult,
    TicketEvidenceMetadata,
    TicketLifecycleService,
)
from ams.application.ticket_queries import TicketReadService
from ams.application.tickets import TicketCreationService
from ams.domain.identity import CurrentActor
from ams.domain.ticket_views import TicketPerspective
from ams.domain.tickets import TicketStatus
from ams.interface.api.dependencies import (
    get_current_actor,
    get_ticket_creation_service,
    get_ticket_lifecycle_service,
    get_ticket_read_service,
)
from ams.interface.api.ticket_models import (
    CreateTicketsRequest,
    CreateTicketsResponse,
    ResolveCancellationRequest,
    TicketActionResponse,
    TicketDetailResponse,
    TicketEvidenceMetadataRequest,
    TicketPageResponse,
    TicketReasonRequest,
)

UPLOAD_CHUNK_SIZE = 64 * 1024
IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key", min_length=1, max_length=255)

router = APIRouter(prefix="/tickets", tags=["Borrow Tickets"])


@router.post(
    "/borrow",
    response_model=CreateTicketsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_borrow_tickets(
    request: CreateTicketsRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketCreationService, Depends(get_ticket_creation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> CreateTicketsResponse:
    """Create valid grouped Tickets and report invalid groups with stable reasons."""

    result = await service.create(request.to_command(), actor, idempotency_key)
    return CreateTicketsResponse.from_result(result)


@router.get("", response_model=TicketPageResponse)
async def list_tickets(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketReadService, Depends(get_ticket_read_service)],
    perspective: Annotated[TicketPerspective, Query()] = TicketPerspective.ALL,
    ticket_status: Annotated[TicketStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    asset_id: Annotated[UUID | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TicketPageResponse:
    """Return a filtered stable page containing only actor-owned Tickets."""

    page = await service.list_tickets(
        actor,
        perspective=perspective,
        status=ticket_status,
        search=search,
        asset_id=asset_id,
        offset=offset,
        limit=limit,
    )
    return TicketPageResponse.from_page(page)


@router.get("/{ticket_id}", response_model=TicketDetailResponse)
async def get_ticket(
    ticket_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketReadService, Depends(get_ticket_read_service)],
) -> TicketDetailResponse:
    """Return ownership-scoped Ticket detail and immutable history."""

    return TicketDetailResponse.from_view(await service.get_ticket(actor, ticket_id))


@router.post("/{ticket_id}/approve", response_model=TicketActionResponse)
async def approve_ticket(
    ticket_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> TicketActionResponse:
    """Approve a pending Ticket as its source custodian."""

    return TicketActionResponse.from_result(
        await service.approve(ticket_id, actor, idempotency_key)
    )


@router.post("/{ticket_id}/reject", response_model=TicketActionResponse)
async def reject_ticket(
    ticket_id: UUID,
    request: TicketReasonRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> TicketActionResponse:
    """Reject a pending Ticket with a required reason."""

    return TicketActionResponse.from_result(
        await service.reject(ticket_id, request.reason, actor, idempotency_key)
    )


@router.post("/{ticket_id}/cancel-request", response_model=TicketActionResponse)
async def request_ticket_cancellation(
    ticket_id: UUID,
    request: TicketReasonRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> TicketActionResponse:
    """Cancel pending directly or request approved-stage cancellation."""

    return TicketActionResponse.from_result(
        await service.request_cancellation(ticket_id, request.reason, actor, idempotency_key)
    )


@router.post("/{ticket_id}/cancel-resolve", response_model=TicketActionResponse)
async def resolve_ticket_cancellation(
    ticket_id: UUID,
    request: ResolveCancellationRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> TicketActionResponse:
    """Resolve an approved-stage cancellation as the source custodian."""

    return TicketActionResponse.from_result(
        await service.resolve_cancellation(ticket_id, request.approve, actor, idempotency_key)
    )


@router.post("/{ticket_id}/confirm-received", response_model=TicketActionResponse)
async def confirm_ticket_received(
    ticket_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> TicketActionResponse:
    """Confirm borrower receipt with one to three private evidence images."""

    return await _run_evidence_action(
        service.confirm_received, ticket_id, metadata, files, actor, idempotency_key
    )


@router.post("/{ticket_id}/mark-returned", response_model=TicketActionResponse)
async def mark_ticket_returned(
    ticket_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> TicketActionResponse:
    """Record borrower return evidence without releasing inventory."""

    return await _run_evidence_action(
        service.mark_returned, ticket_id, metadata, files, actor, idempotency_key
    )


@router.post("/{ticket_id}/confirm-returned", response_model=TicketActionResponse)
async def confirm_ticket_returned(
    ticket_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TicketLifecycleService, Depends(get_ticket_lifecycle_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> TicketActionResponse:
    """Confirm source-custodian receipt and release the temporary loan."""

    return await _run_evidence_action(
        service.confirm_returned, ticket_id, metadata, files, actor, idempotency_key
    )


async def _run_evidence_action(
    action: Callable[
        [UUID, TicketEvidenceMetadata, tuple[EvidenceFileInput, ...], CurrentActor, str],
        Awaitable[TicketActionResult],
    ],
    ticket_id: UUID,
    metadata_json: str,
    files: list[UploadFile],
    actor: CurrentActor,
    idempotency_key: str,
) -> TicketActionResponse:
    """Parse multipart input and invoke one typed lifecycle method."""

    parsed = _parse_metadata(metadata_json)
    inputs = tuple(
        EvidenceFileInput(_upload_content(upload), upload.content_type or "")
        for upload in files
    )
    result = await action(
        ticket_id, TicketEvidenceMetadata(parsed.note), inputs, actor, idempotency_key
    )
    return TicketActionResponse.from_result(result)


async def _upload_content(upload: UploadFile) -> AsyncIterable[bytes]:
    while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
        yield chunk


def _parse_metadata(value: str) -> TicketEvidenceMetadataRequest:
    try:
        return TicketEvidenceMetadataRequest.model_validate_json(value)
    except ValidationError as exception:
        raise RequestValidationError(exception.errors()) from exception
