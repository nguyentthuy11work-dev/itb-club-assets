"""Global conversion of failures into the stable public API error envelope."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ams.application.asset_creation import AssetReferenceNotFoundError
from ams.application.asset_imports import (
    ImportPreviewUnavailableError,
    UnsupportedImportFileError,
)
from ams.application.asset_queries import AssetReadNotFoundError, TeamAssetScopeRequiredError
from ams.application.custody_confirmation import CustodyResourceNotFoundError
from ams.application.event_days import EventDayNotFoundError, EventPersistenceError
from ams.application.event_reminders import EventReminderError
from ams.application.evidence import EvidenceValidationError
from ams.application.health import ServiceUnavailableError
from ams.application.idempotency import IdempotencyConflictError
from ams.application.member_roles import MemberNotFoundError
from ams.application.operational_queries import (
    InvalidTransactionRangeError,
    OperationalReadNotFoundError,
    OperationalReadPersistenceError,
)
from ams.application.responsible_team import (
    ResponsibleTeamAssetNotFoundError,
    ResponsibleTeamNotFoundError,
    ResponsibleTeamUpdateError,
)
from ams.application.ticket_commands import (
    TicketCommandError,
    TicketResourceNotFoundError,
)
from ams.application.ticket_queries import TicketReadNotFoundError
from ams.application.tickets import TicketPersistenceError
from ams.domain.assets import AssetCustodyError, CustodyConfirmationForbiddenError
from ams.domain.authentication import AccountInactiveError, AuthenticationError
from ams.domain.events import (
    EventConfigurationError,
    EventReferenceError,
    EventScheduleConflictError,
)
from ams.domain.handovers import (
    HandoverAlreadyExistsError,
    HandoverConfirmationForbiddenError,
    HandoverDataError,
    HandoverExportForbiddenError,
    HandoverExportLimitError,
    HandoverNotFoundError,
    HandoverPersistenceError,
    HandoverRoleRequiredError,
    InvalidHandoverTransitionError,
    TermNotEndedError,
    TermNotFoundError,
)
from ams.domain.identity import AuthorizationError
from ams.domain.reconciliations import (
    InvalidReconciliationDateError,
    InvalidReconciliationTransitionError,
    ReconciliationCloseBlockedError,
    ReconciliationDataError,
    ReconciliationForbiddenError,
    ReconciliationNotFoundError,
    ReconciliationPersistenceError,
)
from ams.domain.shift_execution import (
    InvalidShiftTransitionError,
    ShiftActionForbiddenError,
    ShiftExecutionError,
    ShiftNotFoundError,
    ShiftPersistenceError,
    ShiftQuantityError,
    ShiftScheduleUnavailableError,
)
from ams.domain.tickets import (
    InvalidTicketTransitionError,
    TicketActionForbiddenError,
    TicketRuleError,
)
from ams.domain.transfers import (
    InvalidTransferTransitionError,
    TransferConfirmationForbiddenError,
    TransferDataError,
    TransferNotFoundError,
    TransferPersistenceError,
    TransferQuantityError,
    TransferRoleRequiredError,
)
from ams.interface.api.models import ErrorDetail, ErrorResponse
from ams.interface.api.observability import TRACE_HEADER

LOGGER = structlog.get_logger("ams.api.errors")


def register_exception_handlers(application: FastAPI) -> None:
    """Install all API-wide exception mappings in one composition function."""

    application.add_exception_handler(AuthenticationError, authentication_error_handler)
    application.add_exception_handler(AuthorizationError, authorization_error_handler)
    application.add_exception_handler(MemberNotFoundError, member_not_found_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(HTTPException, http_error_handler)
    application.add_exception_handler(ServiceUnavailableError, unavailable_error_handler)
    application.add_exception_handler(IdempotencyConflictError, idempotency_error_handler)
    application.add_exception_handler(AssetCustodyError, asset_custody_error_handler)
    application.add_exception_handler(
        AssetReferenceNotFoundError, asset_reference_error_handler
    )
    application.add_exception_handler(AssetReadNotFoundError, asset_read_error_handler)
    application.add_exception_handler(TeamAssetScopeRequiredError, team_scope_error_handler)
    application.add_exception_handler(
        UnsupportedImportFileError, unsupported_import_error_handler
    )
    application.add_exception_handler(
        ImportPreviewUnavailableError, import_preview_error_handler
    )
    application.add_exception_handler(
        CustodyConfirmationForbiddenError, custody_forbidden_error_handler
    )
    application.add_exception_handler(
        CustodyResourceNotFoundError, custody_not_found_error_handler
    )
    application.add_exception_handler(EvidenceValidationError, evidence_error_handler)
    application.add_exception_handler(
        ResponsibleTeamUpdateError, responsible_team_update_error_handler
    )
    application.add_exception_handler(
        TicketActionForbiddenError, ticket_forbidden_error_handler
    )
    application.add_exception_handler(
        InvalidTicketTransitionError, ticket_transition_error_handler
    )
    application.add_exception_handler(TicketRuleError, ticket_rule_error_handler)
    application.add_exception_handler(
        TicketResourceNotFoundError, ticket_not_found_error_handler
    )
    application.add_exception_handler(TicketReadNotFoundError, ticket_not_found_error_handler)
    application.add_exception_handler(TicketCommandError, ticket_command_error_handler)
    application.add_exception_handler(TicketPersistenceError, ticket_persistence_error_handler)
    application.add_exception_handler(
        OperationalReadNotFoundError, operational_not_found_error_handler
    )
    application.add_exception_handler(
        InvalidTransactionRangeError, operational_range_error_handler
    )
    application.add_exception_handler(
        OperationalReadPersistenceError, operational_persistence_error_handler
    )
    application.add_exception_handler(EventDayNotFoundError, event_not_found_error_handler)
    application.add_exception_handler(EventReferenceError, event_not_found_error_handler)
    application.add_exception_handler(EventScheduleConflictError, event_schedule_error_handler)
    application.add_exception_handler(EventReminderError, event_schedule_error_handler)
    application.add_exception_handler(
        EventConfigurationError, event_configuration_error_handler
    )
    application.add_exception_handler(EventPersistenceError, event_persistence_error_handler)
    application.add_exception_handler(ShiftNotFoundError, shift_not_found_error_handler)
    application.add_exception_handler(ShiftActionForbiddenError, shift_forbidden_error_handler)
    application.add_exception_handler(InvalidShiftTransitionError, shift_conflict_error_handler)
    application.add_exception_handler(
        ShiftScheduleUnavailableError, shift_conflict_error_handler
    )
    application.add_exception_handler(ShiftQuantityError, shift_quantity_error_handler)
    application.add_exception_handler(ShiftExecutionError, shift_quantity_error_handler)
    application.add_exception_handler(ShiftPersistenceError, shift_persistence_error_handler)
    application.add_exception_handler(
        ReconciliationNotFoundError, reconciliation_not_found_error_handler
    )
    application.add_exception_handler(
        ReconciliationForbiddenError, reconciliation_forbidden_error_handler
    )
    application.add_exception_handler(
        InvalidReconciliationTransitionError, reconciliation_conflict_error_handler
    )
    application.add_exception_handler(
        ReconciliationCloseBlockedError, reconciliation_conflict_error_handler
    )
    application.add_exception_handler(
        InvalidReconciliationDateError, reconciliation_data_error_handler
    )
    application.add_exception_handler(
        ReconciliationDataError, reconciliation_data_error_handler
    )
    application.add_exception_handler(
        ReconciliationPersistenceError, reconciliation_persistence_error_handler
    )
    application.add_exception_handler(TransferNotFoundError, transfer_not_found_error_handler)
    application.add_exception_handler(
        TransferConfirmationForbiddenError, transfer_forbidden_error_handler
    )
    application.add_exception_handler(
        TransferRoleRequiredError, transfer_forbidden_error_handler
    )
    application.add_exception_handler(
        InvalidTransferTransitionError, transfer_conflict_error_handler
    )
    application.add_exception_handler(TransferQuantityError, transfer_conflict_error_handler)
    application.add_exception_handler(TransferDataError, transfer_data_error_handler)
    application.add_exception_handler(
        TransferPersistenceError, transfer_persistence_error_handler
    )
    application.add_exception_handler(HandoverNotFoundError, handover_not_found_handler)
    application.add_exception_handler(TermNotFoundError, term_not_found_handler)
    application.add_exception_handler(
        HandoverConfirmationForbiddenError, handover_forbidden_handler
    )
    application.add_exception_handler(HandoverExportForbiddenError, handover_forbidden_handler)
    application.add_exception_handler(HandoverRoleRequiredError, handover_forbidden_handler)
    application.add_exception_handler(InvalidHandoverTransitionError, handover_conflict_handler)
    application.add_exception_handler(HandoverAlreadyExistsError, handover_conflict_handler)
    application.add_exception_handler(TermNotEndedError, handover_conflict_handler)
    application.add_exception_handler(HandoverExportLimitError, handover_limit_handler)
    application.add_exception_handler(HandoverDataError, handover_data_handler)
    application.add_exception_handler(HandoverPersistenceError, handover_persistence_handler)
    application.add_exception_handler(Exception, unexpected_error_handler)


async def authentication_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map authentication and inactive-account failures to 401/403."""

    error = exception
    assert isinstance(error, AuthenticationError)
    status_code = 403 if isinstance(error, AccountInactiveError) else 401
    return error_response(request, status_code, error.code, str(error))


async def authorization_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map authenticated policy denial without exposing protected resources."""

    error = exception
    assert isinstance(error, AuthorizationError)
    code = getattr(error, "code", "AUTHORIZATION_DENIED")
    return error_response(request, 403, code, str(error))


async def member_not_found_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map an unknown role-change target to not found."""

    error = exception
    assert isinstance(error, MemberNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def validation_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Return field locations and rule types without echoing unsafe input values."""

    error = exception
    assert isinstance(error, RequestValidationError)
    issues = [
        {"location": [str(part) for part in issue["loc"]], "type": issue["type"]}
        for issue in error.errors()
    ]
    return error_response(
        request,
        422,
        "VALIDATION_ERROR",
        "The request did not match the required schema.",
        {"issues": issues},
    )


async def http_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Normalize framework HTTP failures such as unknown routes and methods."""

    error = exception
    assert isinstance(error, HTTPException)
    code, message = _http_error_contract(error.status_code)
    return error_response(request, error.status_code, code, message)


async def unavailable_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Hide dependency and driver details behind a stable 503 response."""

    assert isinstance(exception, ServiceUnavailableError)
    LOGGER.warning("required_service_unavailable")
    return error_response(
        request,
        503,
        "SERVICE_UNAVAILABLE",
        "A required service is temporarily unavailable.",
    )


async def idempotency_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map idempotency key reuse or committed in-progress state to conflict."""

    error = exception
    assert isinstance(error, IdempotencyConflictError)
    return error_response(request, 409, error.code, str(error))


async def asset_custody_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map expected Asset/Custody business-rule rejection to unprocessable input."""

    error = exception
    assert isinstance(error, AssetCustodyError)
    return error_response(request, 422, error.code, str(error))


async def asset_reference_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map absent or inactive Asset registration references to not found."""

    error = exception
    assert isinstance(error, AssetReferenceNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def asset_read_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map an unavailable Asset read model to the frozen not-found response."""

    error = exception
    assert isinstance(error, AssetReadNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def team_scope_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Reject actors without an explicit current Team Head membership scope."""

    error = exception
    assert isinstance(error, TeamAssetScopeRequiredError)
    return error_response(request, 403, error.code, str(error))


async def unsupported_import_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map unsupported or malformed baseline files to unprocessable input."""

    error = exception
    assert isinstance(error, UnsupportedImportFileError)
    return error_response(request, 422, error.code, str(error))


async def import_preview_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map expired, completed, foreign, or stale previews to conflict."""

    error = exception
    assert isinstance(error, ImportPreviewUnavailableError)
    return error_response(request, 409, error.code, str(error))


async def custody_forbidden_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map an actor confirming someone else's Custody to forbidden."""

    error = exception
    assert isinstance(error, CustodyConfirmationForbiddenError)
    return error_response(request, 403, error.code, str(error))


async def custody_not_found_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide whether a missing Custody or owning Asset caused lookup failure."""

    error = exception
    assert isinstance(error, CustodyResourceNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def evidence_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map invalid evidence count/content/size to unprocessable input."""

    error = exception
    assert isinstance(error, EvidenceValidationError)
    return error_response(request, 422, error.code, str(error))


async def responsible_team_update_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map unavailable Team or Asset classification references to not found."""

    error = exception
    assert isinstance(error, (ResponsibleTeamAssetNotFoundError, ResponsibleTeamNotFoundError))
    return error_response(request, 404, error.code, str(error))


async def ticket_forbidden_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map wrong-side Ticket actions to forbidden without mutating state."""

    error = exception
    assert isinstance(error, TicketActionForbiddenError)
    return error_response(request, 403, error.code, str(error))


async def ticket_transition_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map stale or repeated Ticket transitions to conflict."""

    error = exception
    assert isinstance(error, InvalidTicketTransitionError)
    return error_response(request, 409, error.code, str(error))


async def ticket_rule_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map other Ticket business-rule failures to unprocessable input."""

    error = exception
    assert isinstance(error, TicketRuleError)
    return error_response(request, 422, error.code, str(error))


async def ticket_not_found_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide whether a Ticket is absent or outside the actor's ownership scope."""

    error = exception
    assert isinstance(error, (TicketResourceNotFoundError, TicketReadNotFoundError))
    return error_response(request, 404, error.code, str(error))


async def ticket_command_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map command conflicts not represented by a domain transition."""

    error = exception
    assert isinstance(error, TicketCommandError)
    return error_response(request, 409, "TICKET_COMMAND_CONFLICT", str(error))


async def ticket_persistence_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide database details behind a stable service-unavailable response."""

    assert isinstance(exception, TicketPersistenceError)
    LOGGER.warning("ticket_persistence_unavailable")
    return error_response(
        request,
        503,
        TicketPersistenceError.code,
        "Ticket persistence is temporarily unavailable.",
    )


async def operational_not_found_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map an absent Asset history target to not found."""

    error = exception
    assert isinstance(error, OperationalReadNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def operational_range_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map ambiguous or reversed history ranges to unprocessable input."""

    error = exception
    assert isinstance(error, InvalidTransactionRangeError)
    return error_response(request, 422, error.code, str(error))


async def operational_persistence_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide operational read driver details behind service unavailable."""

    error = exception
    assert isinstance(error, OperationalReadPersistenceError)
    LOGGER.warning("operational_read_unavailable")
    return error_response(
        request,
        503,
        error.code,
        "Operational data is temporarily unavailable.",
    )


async def event_not_found_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Hide whether an Event or one of its references is absent/inactive."""

    error = exception
    assert isinstance(error, (EventDayNotFoundError, EventReferenceError))
    return error_response(request, 404, error.code, str(error))


async def event_schedule_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map unsafe state/time replacement and reminder conflicts to 409."""

    error = exception
    assert isinstance(error, (EventScheduleConflictError, EventReminderError))
    return error_response(request, 409, error.code, str(error))


async def event_configuration_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map Event aggregate input violations to unprocessable input."""

    error = exception
    assert isinstance(error, EventConfigurationError)
    return error_response(request, 422, error.code, str(error))


async def event_persistence_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide Event SQL/driver details behind service unavailable."""

    assert isinstance(exception, EventPersistenceError)
    LOGGER.warning("event_persistence_unavailable")
    return error_response(
        request,
        503,
        EventPersistenceError.code,
        "Event configuration persistence is temporarily unavailable.",
    )


async def shift_not_found_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map unavailable Shift execution resources to not found."""

    error = exception
    assert isinstance(error, ShiftNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def shift_forbidden_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Reject inactive, unassigned, or non-Shift-Lead actors."""

    error = exception
    assert isinstance(error, ShiftActionForbiddenError)
    return error_response(request, 403, error.code, str(error))


async def shift_conflict_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map state, schedule, and predecessor gates to conflict."""

    error = exception
    assert isinstance(error, (InvalidShiftTransitionError, ShiftScheduleUnavailableError))
    return error_response(request, 409, error.code, str(error))


async def shift_quantity_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map complete-set and quantity formula violations to unprocessable input."""

    error = exception
    assert isinstance(error, ShiftExecutionError)
    return error_response(request, 422, error.code, str(error))


async def shift_persistence_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide Shift SQL and driver details behind service unavailable."""

    assert isinstance(exception, ShiftPersistenceError)
    LOGGER.warning("shift_persistence_unavailable")
    return error_response(
        request,
        503,
        ShiftPersistenceError.code,
        "Shift execution persistence is temporarily unavailable.",
    )


async def reconciliation_not_found_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide whether a reconciliation session or item caused lookup failure."""

    error = exception
    assert isinstance(error, ReconciliationNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def reconciliation_forbidden_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map HR or snapshot-custodian authorization failures to forbidden."""

    error = exception
    assert isinstance(error, ReconciliationForbiddenError)
    return error_response(request, 403, error.code, str(error))


async def reconciliation_conflict_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map state and close-gate failures to conflict."""

    error = exception
    assert isinstance(
        error,
        (InvalidReconciliationTransitionError, ReconciliationCloseBlockedError),
    )
    return error_response(request, 409, error.code, str(error))


async def reconciliation_data_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map invalid dates, quantities, reasons, and evidence sets to 422."""

    error = exception
    assert isinstance(error, (InvalidReconciliationDateError, ReconciliationDataError))
    return error_response(request, 422, error.code, str(error))


async def reconciliation_persistence_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide reconciliation SQL and driver details behind service unavailable."""

    assert isinstance(exception, ReconciliationPersistenceError)
    LOGGER.warning("reconciliation_persistence_unavailable")
    return error_response(
        request,
        503,
        ReconciliationPersistenceError.code,
        "Reconciliation persistence is temporarily unavailable.",
    )


async def transfer_not_found_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map unavailable Transfer resources to not found."""

    error = exception
    assert isinstance(error, TransferNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def transfer_forbidden_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map HR and participant-side authorization failures to forbidden."""

    error = exception
    assert isinstance(error, (TransferConfirmationForbiddenError, TransferRoleRequiredError))
    return error_response(request, 403, error.code, str(error))


async def transfer_conflict_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Map stale confirmation and unavailable quantity to conflict."""

    error = exception
    assert isinstance(error, (InvalidTransferTransitionError, TransferQuantityError))
    return error_response(request, 409, error.code, str(error))


async def transfer_data_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map malformed Transfer identities or evidence to unprocessable input."""

    error = exception
    assert isinstance(error, TransferDataError)
    return error_response(request, 422, error.code, str(error))


async def transfer_persistence_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    """Hide Transfer SQL and driver details behind service unavailable."""

    assert isinstance(exception, TransferPersistenceError)
    LOGGER.warning("transfer_persistence_unavailable")
    return error_response(
        request,
        503,
        TransferPersistenceError.code,
        "Transfer persistence is temporarily unavailable.",
    )


async def handover_not_found_handler(request: Request, exception: Exception) -> JSONResponse:
    """Hide absent and out-of-scope Handovers behind one not-found response."""

    error = exception
    assert isinstance(error, HandoverNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def term_not_found_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map unavailable configured terms to not found."""

    error = exception
    assert isinstance(error, TermNotFoundError)
    return error_response(request, 404, error.code, str(error))


async def handover_forbidden_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map role, side ownership, and export authorization failures to forbidden."""

    error = exception
    assert isinstance(
        error,
        (
            HandoverConfirmationForbiddenError,
            HandoverExportForbiddenError,
            HandoverRoleRequiredError,
        ),
    )
    return error_response(request, 403, error.code, str(error))


async def handover_conflict_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map duplicate snapshots, premature terms, and stale transitions to conflict."""

    error = exception
    assert isinstance(
        error,
        (
            InvalidHandoverTransitionError,
            HandoverAlreadyExistsError,
            TermNotEndedError,
        ),
    )
    return error_response(request, 409, error.code, str(error))


async def handover_limit_handler(request: Request, exception: Exception) -> JSONResponse:
    """Reject a synchronous export outside the approved bounded policy."""

    error = exception
    assert isinstance(error, HandoverExportLimitError)
    return error_response(request, 413, error.code, str(error))


async def handover_data_handler(request: Request, exception: Exception) -> JSONResponse:
    """Map malformed term and snapshot data to unprocessable input."""

    error = exception
    assert isinstance(error, HandoverDataError)
    return error_response(request, 422, error.code, str(error))


async def handover_persistence_handler(request: Request, exception: Exception) -> JSONResponse:
    """Hide Handover SQL and driver details behind service unavailable."""

    assert isinstance(exception, HandoverPersistenceError)
    LOGGER.warning("handover_persistence_unavailable")
    return error_response(
        request,
        503,
        HandoverPersistenceError.code,
        "Handover persistence is temporarily unavailable.",
    )


async def unexpected_error_handler(request: Request, exception: Exception) -> JSONResponse:
    """Log an unexpected failure and return no implementation details."""

    LOGGER.exception("unhandled_api_error", exception_type=type(exception).__name__)
    return error_response(
        request,
        500,
        "INTERNAL_SERVER_ERROR",
        "An unexpected error occurred.",
    )


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> JSONResponse:
    """Build the canonical error body using the request correlation ID."""

    trace_id = _trace_id(request)
    content = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=dict(details or {})),
        trace_id=trace_id,
    ).model_dump(mode="json", by_alias=True)
    headers = {TRACE_HEADER: str(trace_id)}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _trace_id(request: Request) -> UUID:
    value = getattr(request.state, "trace_id", None)
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    return uuid4()


def _http_error_contract(status_code: int) -> tuple[str, str]:
    contracts = {
        404: ("RESOURCE_NOT_FOUND", "The requested resource was not found."),
        405: ("METHOD_NOT_ALLOWED", "The HTTP method is not allowed for this resource."),
    }
    return contracts.get(
        status_code,
        ("HTTP_ERROR", "The request could not be completed."),
    )
