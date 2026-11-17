import logging
from collections.abc import Sequence

from app.channels.base import Message, Notifier
from app.config.settings import Channel
from app.errors import ChannelRejectedError, ChannelUnavailableError

logger = logging.getLogger(__name__)


class Dispatcher:
    """Tries each channel in order (primary first) until one accepts the message. The `copy` channel (email)
    also gets every message another channel delivered: it is the fixed record, not only the fallback."""

    def __init__(self, channels: Sequence[Notifier], *, copy: Channel | None = None) -> None:
        self._channels = list(channels)
        self._copy = copy

    async def deliver(self, message: Message, *, copy: bool = True) -> Channel | None:
        """The channel that accepted the message, or None when every channel failed. copy=False skips the
        copy: a reply to a chat command."""
        failures: list[str] = []
        for index, channel in enumerate(self._channels):
            failure = await self._send(channel, message)
            if failure is not None:
                failures.append(failure)
                continue
            if failures:
                logger.info("Delivered by the fallback channel %s", channel.name)
            # A copy channel tried before this one already failed: no second attempt.
            later = (c for c in self._channels[index + 1 :] if copy and c.name == self._copy)
            if (extra := next(later, None)) is not None and (failure := await self._send(extra, message)):
                logger.warning("Copy not delivered, jobs=%s %s", ",".join(message.keys) or "-", failure)
            return channel.name
        logger.error("NOTIFY_FAILED jobs=%s %s", ",".join(message.keys) or "-", " ".join(failures))
        return None

    async def _send(self, channel: Notifier, message: Message) -> str | None:
        """None when the channel accepted the message, else why it did not."""
        try:
            await channel.send(message)
        except ChannelRejectedError as e:
            logger.error("Channel %s rejected the message: %s", channel.name, e)
            return f"{channel.name}=rejected({e})"
        except ChannelUnavailableError as e:
            logger.warning("Channel %s unavailable: %s", channel.name, e)
            return f"{channel.name}=unavailable({e})"
        except Exception as e:
            logger.exception("Channel %s failed unexpectedly", channel.name)
            return f"{channel.name}=error({type(e).__name__})"
        return None
