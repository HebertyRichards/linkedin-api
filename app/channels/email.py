import logging

import httpx

from app.channels.base import Message
from app.config.settings import Channel, Settings
from app.errors import ChannelRejectedError, ChannelUnavailableError, RetryableError
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.mask import mask_email, redact
from app.utils.retry import parse_retry_after, retrying

logger = logging.getLogger(__name__)

# HTTP API instead of SMTP: Railway's egress drops ports 465 and 587 on the current plan.
_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10.0
_ATTEMPTS = 3


def _provider_message(response: httpx.Response) -> str:
    # The body is the only place where Resend says why (unverified domain, sender, key permission).
    try:
        message = response.json().get("message")
    except ValueError:
        message = None
    return redact(str(message) if message else response.text[:300])


class EmailNotifier:
    """Sends through the Resend HTTP API."""

    name: Channel = "email"

    def __init__(self, client: httpx.AsyncClient, settings: Settings, *, retry_base_delay: float = 1.0) -> None:
        self._client = client
        self._api_key = settings.resend_api_key
        self._sender = settings.email_from
        self._recipient = settings.email_to
        self._retry_base_delay = retry_base_delay
        self._breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0, name="resend")

    async def send(self, message: Message) -> None:
        if not self._breaker.allow():
            raise ChannelUnavailableError("circuit open")
        try:
            response = await retrying(
                lambda: self._post(message), attempts=_ATTEMPTS, base_delay=self._retry_base_delay
            )
        except RetryableError as e:
            self._breaker.record_failure()
            raise ChannelUnavailableError(str(e)) from e

        self._breaker.record_success()
        if response.is_error:
            raise ChannelRejectedError(f"status {response.status_code}: {_provider_message(response)}")
        logger.info("Email accepted for %s", mask_email(self._recipient))

    async def _post(self, message: Message) -> httpx.Response:
        try:
            response = await self._client.post(
                _ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Idempotency-Key": message.idempotency_key,
                },
                json={
                    "from": self._sender,
                    "to": [self._recipient],
                    "subject": message.subject,
                    "html": message.html,
                    "text": message.text,
                },
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            # Safe to repeat: the idempotency key makes Resend answer a repeat with the first email.
            raise RetryableError(f"network error: {type(e).__name__}") from e
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(
                f"status {response.status_code}", parse_retry_after(response.headers.get("retry-after"))
            )
        return response
