from typing import Protocol

from app.schemas.job import Job


class JobSource(Protocol):
    """Where jobs come from. Swapping the provider is a new class with this shape, wired in main.py."""

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int) -> list[Job]:
        """Easy Apply jobs posted in the last `window_seconds` for one search. Raises SourceUnavailableError."""
        ...
