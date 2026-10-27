import logging
from collections.abc import Sequence

from app.channels.base import Message, Notifier
from app.config.settings import Channel
from app.errors import ChannelRejectedError, ChannelUnavailableError

logger = logging.getLogger(__name__)


class Dispatcher:
    """Tries each channel in order (primary first) until one accepts the message."""

    def __init__(self, channels: Sequence[Notifier]) -> None:
        self._channels = list(channels)

    async def deliver(self, message: Message) -> Channel | None:
        """The channel that accepted the message, or None when every channel failed."""
        failures: list[str] = []
        for channel in self._channels:
            try:
                await channel.send(message)
            except ChannelRejectedError as e:
                logger.error("Channel %s rejected the message: %s", channel.name, e)
                failures.append(f"{channel.name}=rejected({e})")
            except ChannelUnavailableError as e:
                logger.warning("Channel %s unavailable: %s", channel.name, e)
                failures.append(f"{channel.name}=unavailable({e})")
            else:
                if failures:
                    logger.info("Delivered by the fallback channel %s", channel.name)
                return channel.name
        logger.error("NOTIFY_FAILED jobs=%s %s", ",".join(message.keys) or "-", " ".join(failures))
        return None
