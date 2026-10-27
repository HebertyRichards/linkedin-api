class SourceUnavailableError(Exception):
    """The job source failed or refused the request. `blocked` marks a rate-limit or block answer."""

    def __init__(self, reason: str, *, blocked: bool = False, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.blocked = blocked
        self.retry_after = retry_after


class RetryableError(Exception):
    """A failed attempt worth repeating: network error, 429 or 5xx."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class ChannelError(Exception):
    """A channel did not accept the message."""


class ChannelUnavailableError(ChannelError):
    """Transient: the provider is down, rate-limited, disconnected or over our own cap."""


class ChannelRejectedError(ChannelError):
    """The provider answered and refused (4xx). Usually configuration: key, sender or recipient."""
