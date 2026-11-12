from dataclasses import dataclass
from typing import Protocol

from app.config.settings import Channel


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes


@dataclass(frozen=True)
class Message:
    subject: str
    text: str  # WhatsApp body and the email's plain-text part
    html: str
    # Same jobs -> same key and same body, so a provider that dedupes (Resend) never sends twice.
    idempotency_key: str
    keys: tuple[str, ...]  # job keys this message covers, for the seen-set and the logs
    attachments: tuple[Attachment, ...] = ()  # sent by email only; WhatsApp gets the text


class Notifier(Protocol):
    name: Channel

    async def send(self, message: Message) -> None:
        """Returns once the provider accepted the message. Raises ChannelUnavailableError or ChannelRejectedError."""
        ...
