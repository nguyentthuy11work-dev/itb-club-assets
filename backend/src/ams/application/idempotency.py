"""Canonical request fingerprints and reusable idempotent command execution."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from ams.application.reliability import (
    IClock,
    IdempotencyRecord,
    IdempotencyStatus,
    StagedFile,
)
from ams.application.unit_of_work import IUnitOfWork


class IdempotencyError(Exception):
    """Base class for expected idempotent-command failures."""


class IdempotencyFingerprintError(IdempotencyError):
    """The request cannot be represented by the canonical fingerprint contract."""

    code = "IDEMPOTENCY_FINGERPRINT_INVALID"


class IdempotencyConflictError(IdempotencyError):
    """The idempotency key cannot be used for this command execution."""

    code = "IDEMPOTENCY_CONFLICT"


class IdempotencyKeyReusedError(IdempotencyConflictError):
    """The same actor/command/key was previously used for another payload."""

    code = "IDEMPOTENCY_KEY_REUSED"


class IdempotencyInProgressError(IdempotencyConflictError):
    """A previously committed record is still marked in progress."""

    code = "IDEMPOTENCY_IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class MultipartFileFingerprint:
    """Content identity for one validated multipart file field."""

    field_name: str
    content_type: str
    content_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        """Reject ambiguous field names, MIME types, hashes, and sizes."""

        if not self.field_name.strip() or not self.content_type.strip():
            raise IdempotencyFingerprintError(
                "Multipart file field and content type must not be blank."
            )
        if self.size_bytes < 0:
            raise IdempotencyFingerprintError("Multipart file size cannot be negative.")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise IdempotencyFingerprintError(
                "Multipart content fingerprint must be lowercase SHA-256."
            )

    @classmethod
    def from_staged(cls, field_name: str, staged_file: StagedFile) -> MultipartFileFingerprint:
        """Create content identity from a validated private staged file."""

        return cls(
            field_name=field_name,
            content_type=staged_file.content_type,
            content_sha256=staged_file.content_sha256,
            size_bytes=staged_file.size_bytes,
        )


@dataclass(frozen=True, slots=True)
class CommandResponse:
    """Successful response produced by an idempotent command operation."""

    status_code: int
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        """Require a successful JSON-object response that can be persisted."""

        if not 200 <= self.status_code <= 299:
            raise IdempotencyError("Idempotent commands must return a 2xx response.")
        _canonical_json(dict(self.body))


@dataclass(frozen=True, slots=True)
class IdempotentCommandResult:
    """Current or replayed successful response returned to the interface layer."""

    status_code: int
    body: Mapping[str, object]
    replayed: bool


IdempotentOperation = Callable[[IUnitOfWork], Awaitable[CommandResponse]]


class IIdempotentCommandExecutor(Protocol):
    """Execute a command once or replay its committed response."""

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        """Execute or replay one actor-scoped command key."""


class IdempotentCommandExecutor:
    """Run one command effect and its replay record in a shared transaction."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], IUnitOfWork],
        clock: IClock,
        *,
        record_ttl: timedelta = timedelta(hours=24),
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if record_ttl <= timedelta(0):
            raise ValueError("Idempotency record TTL must be positive.")
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._record_ttl = record_ttl
        self._id_factory = id_factory

    async def execute(
        self,
        *,
        actor_id: UUID,
        command_name: str,
        idempotency_key: str,
        request_fingerprint: str,
        operation: IdempotentOperation,
    ) -> IdempotentCommandResult:
        """Execute once or return the atomically stored response for an exact retry."""

        started_at = self._clock.now()
        candidate = IdempotencyRecord(
            id=self._id_factory(),
            actor_id=actor_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            status=IdempotencyStatus.IN_PROGRESS,
            expires_at=started_at + self._record_ttl,
            created_at=started_at,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            record = await unit_of_work.idempotency.acquire(candidate)
            if record.request_fingerprint != request_fingerprint:
                raise IdempotencyKeyReusedError(
                    "The idempotency key was already used for another request payload."
                )
            if record.status is IdempotencyStatus.SUCCEEDED:
                assert record.response_status_code is not None
                assert record.response_body is not None
                return IdempotentCommandResult(
                    status_code=record.response_status_code,
                    body=dict(record.response_body),
                    replayed=True,
                )
            if record.id != candidate.id:
                raise IdempotencyInProgressError(
                    "The idempotent command is already in progress."
                )

            response = await operation(unit_of_work)
            completed_at = self._clock.now()
            record.succeed(
                response_status_code=response.status_code,
                response_body=response.body,
                completed_at=completed_at,
            )
            await unit_of_work.idempotency.save(record)
            await unit_of_work.commit()
            return IdempotentCommandResult(
                status_code=response.status_code,
                body=dict(response.body),
                replayed=False,
            )


def canonical_json_fingerprint(payload: object) -> str:
    """Return SHA-256 of deterministic UTF-8 JSON with sorted object keys."""

    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def multipart_fingerprint(
    fields: Mapping[str, object],
    files: Sequence[MultipartFileFingerprint],
) -> str:
    """Fingerprint canonical form fields and validated file content identities."""

    ordered_files = sorted(
        files,
        key=lambda item: (
            item.field_name,
            item.content_type,
            item.content_sha256,
            item.size_bytes,
        ),
    )
    manifest: dict[str, object] = {
        "fields": dict(fields),
        "files": [
            {
                "fieldName": item.field_name,
                "contentType": item.content_type,
                "contentSha256": item.content_sha256,
                "sizeBytes": item.size_bytes,
            }
            for item in ordered_files
        ],
    }
    return canonical_json_fingerprint(manifest)


def _canonical_json(payload: object) -> bytes:
    normalized = _normalize_json_value(payload)
    value = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return value.encode("utf-8")


def _normalize_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise IdempotencyFingerprintError("JSON numbers must be finite.")
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise IdempotencyFingerprintError("JSON object keys must be strings.")
            normalized[key] = _normalize_json_value(item)
        return normalized
    raise IdempotencyFingerprintError(
        "Payload must contain only JSON objects, arrays, and scalar values."
    )
