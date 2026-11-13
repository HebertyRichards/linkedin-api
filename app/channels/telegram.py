import asyncio
import json
import logging
from collections.abc import Sequence

import httpx

from app.channels.base import Message
from app.config.settings import Channel, Settings
from app.errors import ChannelRejectedError, ChannelUnavailableError, RetryableError
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.mask import redact
from app.utils.retry import retrying

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_TIMEOUT_SECONDS = 15.0
_ATTEMPTS = 3
MAX_TEXT = 4096  # sendMessage limit, counted after the HTML is parsed: the raw length is never smaller
MAX_CAPTION = 1024
# Telegram asks for at most one message per second in one chat.
FILE_SPACING_SECONDS = 1.0
# A longer 429 wait would hold the whole cycle: the next channel takes the message instead.
_MAX_WAIT_SECONDS = 30.0
# Raised before the request is written: nothing reached Telegram, so repeating cannot duplicate.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_NO_PREVIEW = json.dumps({"is_disabled": True})


def chunks(blocks: Sequence[str], limit: int = MAX_TEXT) -> list[str]:
    """Joins the blocks (one per job) into as few messages as fit the limit, never splitting a block."""
    parts: list[str] = []
    for block in blocks:
        if parts and len(parts[-1]) + 2 + len(block) <= limit:
            parts[-1] += "\n\n" + block
        else:
            parts.append(block)
    return parts


def _description(response: httpx.Response) -> str:
    try:
        description = response.json().get("description")
    except ValueError:
        description = None
    return redact(str(description) if description else response.text[:300])


def _retry_after(response: httpx.Response) -> float | None:
    try:
        value = response.json()["parameters"]["retry_after"]
    except (ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, int | float) else None


class TelegramNotifier:
    """Sends through the Bot API, only ever to TELEGRAM_CHAT_ID. Send only: it never reads updates.

    The text goes first, then each tailored resume as a file. Any failure fails the whole message, so the
    next channel delivers it complete: a part repeated on Telegram is better than a job or a resume lost.
    """

    name: Channel = "telegram"

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        retry_base_delay: float = 1.0,
        file_spacing: float = FILE_SPACING_SECONDS,
    ) -> None:
        self._client = client
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._retry_base_delay = retry_base_delay
        self._file_spacing = file_spacing
        self._breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0, name="telegram")

    async def send(self, message: Message) -> None:
        if not self._breaker.allow():
            raise ChannelUnavailableError("circuit open")
        texts = chunks(message.telegram_blocks)
        try:
            for text in texts:
                await self._call(
                    "sendMessage",
                    {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": _NO_PREVIEW},
                )
            for attachment in message.attachments:
                await asyncio.sleep(self._file_spacing)
                await self._call(
                    "sendDocument",
                    {"chat_id": self._chat_id, "caption": attachment.label[:MAX_CAPTION]},
                    {"document": (attachment.filename, attachment.content, "application/pdf")},
                )
        except (ChannelRejectedError, ChannelUnavailableError):
            self._breaker.record_success()  # Telegram answered: the refusal or the wait is not an outage
            raise
        except RetryableError as e:
            self._breaker.record_failure()
            raise ChannelUnavailableError(str(e)) from e
        except httpx.HTTPError as e:
            # The request may have reached Telegram: repeating could deliver it twice.
            # Never str(e): the URL holds the token.
            self._breaker.record_failure()
            raise ChannelUnavailableError(f"no answer: {type(e).__name__}") from e

        self._breaker.record_success()
        logger.info("Telegram accepted: %d message(s), %d file(s)", len(texts), len(message.attachments))

    async def _call(
        self, method: str, data: dict[str, str], files: dict[str, tuple[str, bytes, str]] | None = None
    ) -> None:
        response = await retrying(
            lambda: self._post(method, data, files), attempts=_ATTEMPTS, base_delay=self._retry_base_delay
        )
        if response.is_error:
            raise ChannelRejectedError(f"{method}: status {response.status_code}: {_description(response)}")

    async def _post(
        self, method: str, data: dict[str, str], files: dict[str, tuple[str, bytes, str]] | None
    ) -> httpx.Response:
        try:
            response = await self._client.post(
                f"{_API}/bot{self._token.get_secret_value()}/{method}",
                data=data,
                files=files,
                timeout=_TIMEOUT_SECONDS,
            )
        except _NOT_SENT as e:
            raise RetryableError(f"{method}: network error: {type(e).__name__}") from e
        if response.status_code == 429:
            wait = _retry_after(response)
            if wait is None or wait > _MAX_WAIT_SECONDS:
                raise ChannelUnavailableError(f"{method}: rate limited (retry after {wait}s)")
            raise RetryableError(f"{method}: status 429", wait)
        if response.status_code >= 500:
            raise RetryableError(f"{method}: status {response.status_code}")
        return response
