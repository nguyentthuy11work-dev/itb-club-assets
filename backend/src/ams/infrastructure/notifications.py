"""Clock, metrics, and provider-neutral email adapters for the outbox worker."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from datetime import UTC, datetime
from threading import Lock

from ams.application.notifications import EmailMessage, IEmailSender, IWorkerMetrics
from ams.application.reliability import IClock


class IEmailProviderClient(ABC):
    """Minimal boundary implemented by the selected production email provider."""

    @abstractmethod
    async def send_email(
        self,
        *,
        recipient_address: str,
        subject: str,
        body: str,
    ) -> str:
        """Return the provider's accepted message identifier."""


class ProviderEmailSender(IEmailSender):
    """Deliver provider-neutral application messages through an injected client."""

    def __init__(self, client: IEmailProviderClient) -> None:
        self._client = client

    async def send(self, message: EmailMessage) -> str:
        """Send one email without leaking recipient data into worker logs."""

        return await self._client.send_email(
            recipient_address=message.recipient_address,
            subject=message.subject,
            body=message.body,
        )


class SystemClock(IClock):
    """Production UTC clock adapter."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant."""

        return datetime.now(UTC)


class InMemoryWorkerMetrics(IWorkerMetrics):
    """Thread-safe metric counters suitable for inspection/export by the host."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, metric_name: str) -> None:
        """Increment a low-cardinality worker outcome counter."""

        with self._lock:
            self._counts[metric_name] += 1

    def value(self, metric_name: str) -> int:
        """Return the current value for host metrics exposure."""

        with self._lock:
            return self._counts[metric_name]
