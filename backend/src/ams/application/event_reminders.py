"""Durable Event Shift open/close reminder scheduling policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ams.application.reliability import IClock, OutboxEvent
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.events import Shift, ShiftStatus
from ams.domain.identity import OperationalRole

EVENT_REMINDER_LEAD = timedelta(minutes=30)
EVENT_REMINDER_TYPES = frozenset({"SHIFT_OPEN_REMINDER", "SHIFT_CLOSE_REMINDER"})


class EventReminderError(Exception):
    """A Shift reminder cannot resolve an active eligible recipient."""

    code = "EVENT_REMINDER_SCHEDULING_FAILED"


class EventReminderScheduler:
    """Synchronize the two approved 30-minute Shift reminder slots."""

    def __init__(
        self,
        clock: IClock,
        *,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory

    async def synchronize(self, unit_of_work: IUnitOfWork, shift: Shift) -> None:
        """Replace future unsent slots or remove them from an ineligible Shift."""

        if shift.status is not ShiftStatus.SCHEDULED:
            await unit_of_work.event_reminders.cancel_unattempted(shift.id)
            return
        lead = await unit_of_work.users.get_identity(shift.shift_lead_user_id)
        if lead is None or not lead.is_active or OperationalRole.SHIFT_LEAD not in lead.roles:
            raise EventReminderError("Shift Lead is unavailable for Event reminders.")
        now = self._clock.now()
        desired = (
            ("SHIFT_OPEN_REMINDER", shift.starts_at - EVENT_REMINDER_LEAD, "OPEN_SHIFT"),
            ("SHIFT_CLOSE_REMINDER", shift.ends_at - EVENT_REMINDER_LEAD, "CLOSE_SHIFT"),
        )
        reminders = tuple(
            self._event(shift, lead.id, lead.email, event_type, scheduled_at, action, now)
            for event_type, scheduled_at, action in desired
            if scheduled_at > now
        )
        await unit_of_work.event_reminders.replace_unattempted(shift.id, reminders)

    def _event(
        self,
        shift: Shift,
        recipient_id: UUID,
        address: str,
        event_type: str,
        scheduled_at: datetime,
        action: str,
        now: datetime,
    ) -> OutboxEvent:
        label = "open" if event_type == "SHIFT_OPEN_REMINDER" else "close"
        return OutboxEvent(
            id=self._id_factory(),
            event_type=event_type,
            aggregate_type="SHIFT",
            aggregate_id=shift.id,
            payload={
                "shiftId": str(shift.id),
                "reminderKind": event_type,
                "reference": {"type": "SHIFT", "id": str(shift.id)},
                "action": action,
                "emails": [
                    {
                        "recipientId": str(recipient_id),
                        "address": address,
                        "subject": f"Shift {label} reminder",
                        "body": f"Shift {shift.name} is due to {label} in 30 minutes.",
                    }
                ],
            },
            scheduled_at=scheduled_at,
            created_at=now,
        )
