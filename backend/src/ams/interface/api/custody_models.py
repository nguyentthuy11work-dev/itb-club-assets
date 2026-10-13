"""Strict API output contracts for evidence-backed Custody confirmation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from ams.application.custody_confirmation import ConfirmCustodyResult
from ams.interface.api.models import StrictApiModel


class ConfirmCustodyResponse(StrictApiModel):
    """Activated Custody state returned after confirmation or exact replay."""

    custody_id: UUID = Field(serialization_alias="custodyId")
    status: Literal["ACTIVE"]
    confirmed_at: datetime = Field(serialization_alias="confirmedAt")

    @classmethod
    def from_result(cls, result: ConfirmCustodyResult) -> ConfirmCustodyResponse:
        """Map application output without exposing object keys or content hashes."""

        return cls(
            custody_id=result.custody_id,
            status="ACTIVE",
            confirmed_at=result.confirmed_at,
        )


class ConfirmCustodyMetadataRequest(StrictApiModel):
    """JSON metadata part submitted alongside confirmation files."""

    note: str | None = Field(default=None, max_length=1000)
