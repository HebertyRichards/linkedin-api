import logging
import time
from collections import deque

import httpx

from app.channels.base import Message
from app.config.settings import Channel, Settings
from app.errors import ChannelRejectedError, ChannelUnavailableError, RetryableError
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.mask import mask_phone, redact
from app.utils.retry import retrying

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15.0
_ATTEMPTS = 3
# Messages the bot starts are what gets a Baileys number banned; a burst looks like bulk sending.
HOURLY_CAP = 6
# Raised before the request is written: nothing reached Evolution, so repeating cannot duplicate.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class WhatsAppNotifier:
    """Sends through the Evolution API, only ever to WHATSAPP_TO."""

    name: Channel = "whatsapp"

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        retry_base_delay: float = 1.0,
        hourly_cap: int = HOURLY_CAP,
    ) -> None:
        self._client = client
        self._base_url = settings.evolution_api_url
        self._instance = settings.evolution_instance
        self._api_key = settings.evolution_api_key
        self._recipient = settings.whatsapp_to
        self._retry_base_delay = retry_base_delay
        self._hourly_cap = hourly_cap
        self._sent_at: deque[float] = deque()
        self._breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0, name="evolution")

    async def send(self, message: Message) -> None:
        if not self._within_hourly_cap():
            raise ChannelUnavailableError(f"hourly cap of {self._hourly_cap} messages reached")
        if not self._breaker.allow():
            raise ChannelUnavailableError("circuit open")
        try:
            state = await self._connection_state()
            if state != "open":
                # Evolution answered; it is the phone session that is down (QR code needs a new scan).
                self._breaker.record_success()
                raise ChannelUnavailableError(f"instance not connected (state: {state})")
            response = await retrying(
                lambda: self._post_text(message.text), attempts=_ATTEMPTS, base_delay=self._retry_base_delay
            )
        except ChannelRejectedError:
            self._breaker.record_success()
            raise
        except RetryableError as e:
            self._breaker.record_failure()
            raise ChannelUnavailableError(str(e)) from e
        except httpx.HTTPError as e:
            # The request may have reached Evolution: repeating could deliver the message twice.
            self._breaker.record_failure()
            raise ChannelUnavailableError(f"no answer: {type(e).__name__}") from e

        self._breaker.record_success()
        if response.is_error:
            raise ChannelRejectedError(f"status {response.status_code}: {redact(response.text[:300])}")
        self._sent_at.append(time.monotonic())
        logger.info("WhatsApp accepted for %s", mask_phone(self._recipient))

    def _within_hourly_cap(self) -> bool:
        hour_ago = time.monotonic() - 3600
        while self._sent_at and self._sent_at[0] < hour_ago:
            self._sent_at.popleft()
        return len(self._sent_at) < self._hourly_cap

    def _headers(self) -> dict[str, str]:
        return {"apikey": self._api_key.get_secret_value()}

    async def _connection_state(self) -> str:
        try:
            response = await self._client.get(
                f"{self._base_url}/instance/connectionState/{self._instance}",
                headers=self._headers(),
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise RetryableError(f"connection state: {type(e).__name__}") from e
        if response.status_code >= 500:
            raise RetryableError(f"connection state: status {response.status_code}")
        if response.is_error:
            raise ChannelRejectedError(
                f"connection state: status {response.status_code}: {redact(response.text[:300])}"
            )
        try:
            state = response.json()["instance"]["state"]
        except (ValueError, KeyError, TypeError):
            return "unknown"
        return state if isinstance(state, str) else "unknown"

    async def _post_text(self, text: str) -> httpx.Response:
        try:
            response = await self._client.post(
                f"{self._base_url}/message/sendText/{self._instance}",
                headers=self._headers(),
                json={"number": self._recipient, "text": text, "linkPreview": False},
                timeout=_TIMEOUT_SECONDS,
            )
        except _NOT_SENT as e:
            raise RetryableError(f"network error: {type(e).__name__}") from e
        if response.status_code >= 500:
            raise RetryableError(f"status {response.status_code}")
        return response
