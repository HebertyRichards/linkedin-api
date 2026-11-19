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
    """Sends through the Bot API, only ever to TELEGRAM_CHAT_ID. The commands are read by TelegramInbox.

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
                    {"document": (attachment.filename, attachment.content, attachment.media_type)},
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


# Long polling: Telegram holds getUpdates open until a message arrives or this many seconds pass.
POLL_SECONDS = 50


class TelegramInbox:
    """Reads the commands sent to the bot and answers them. Only a message from TELEGRAM_CHAT_ID counts:
    one from any other chat (anyone who finds the bot) is dropped without an answer."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings, *, poll_seconds: int = POLL_SECONDS) -> None:
        self._client = client
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._poll_seconds = poll_seconds
        self._offset: int | None = None

    async def receive(self) -> list[str]:
        """The texts sent from TELEGRAM_CHAT_ID since the last call. Raises ChannelUnavailableError."""
        if self._offset is None:
            # A command sent while the worker was down is dropped: a deploy never runs one.
            last = await self._updates(offset=-1, poll_timeout=0)
            last_id = last[-1].get("update_id") if last else None
            self._offset = last_id + 1 if isinstance(last_id, int) else 0
            return []
        texts: list[str] = []
        for update in await self._updates(offset=self._offset, poll_timeout=self._poll_seconds):
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            self._offset = max(self._offset, update_id + 1)
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict) or str(chat.get("id")) != self._chat_id:
                logger.info("Telegram: message from another chat ignored")
                continue
            if isinstance(message.get("text"), str):
                texts.append(message["text"])
        return texts

    async def reply(self, text: str) -> None:
        """Plain text to TELEGRAM_CHAT_ID. Raises ChannelUnavailableError."""
        response = await self._post("sendMessage", {"chat_id": self._chat_id, "text": text}, _TIMEOUT_SECONDS)
        if response.is_error:
            raise ChannelUnavailableError(f"sendMessage: status {response.status_code}: {_description(response)}")

    async def _updates(self, *, offset: int, poll_timeout: int) -> list[dict[str, object]]:
        data = {"offset": str(offset), "timeout": str(poll_timeout), "allowed_updates": '["message"]'}
        response = await self._post("getUpdates", data, request_timeout=poll_timeout + _TIMEOUT_SECONDS)
        if response.status_code == 409:
            raise ChannelUnavailableError("getUpdates: another instance reads this bot (409)")
        if response.is_error:
            raise ChannelUnavailableError(f"getUpdates: status {response.status_code}: {_description(response)}")
        try:
            payload = response.json()
        except ValueError as e:
            raise ChannelUnavailableError("getUpdates: unexpected answer") from e
        if not isinstance(payload, dict):
            raise ChannelUnavailableError("getUpdates: unexpected answer")
        result = payload.get("result")
        if not isinstance(result, list):
            raise ChannelUnavailableError("getUpdates: unexpected answer")
        updates: list[dict[str, object]] = []
        for raw in result:
            if not isinstance(raw, dict):
                raise ChannelUnavailableError("getUpdates: unexpected answer")
            update: dict[str, object] = {}
            for key, value in raw.items():
                if not isinstance(key, str):
                    raise ChannelUnavailableError("getUpdates: unexpected answer")
                update[key] = value
            if not isinstance(update.get("update_id"), int):
                raise ChannelUnavailableError("getUpdates: unexpected answer")
            updates.append(update)
        return updates

    async def _post(self, method: str, data: dict[str, str], request_timeout: float) -> httpx.Response:
        try:
            return await self._client.post(
                f"{_API}/bot{self._token.get_secret_value()}/{method}", data=data, timeout=request_timeout
            )
        except httpx.HTTPError as e:
            # Never str(e): the URL holds the token.
            raise ChannelUnavailableError(f"{method}: no answer: {type(e).__name__}") from e
