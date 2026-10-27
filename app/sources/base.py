from typing import Protocol

from app.schemas.job import Job


class JobSource(Protocol):
    """Where jobs come from. Swapping the provider is a new class with this shape, wired in main.py."""

    async def fetch_recent(self, keywords: str, location: str) -> list[Job]:
        """Easy Apply jobs posted recently for one search. Raises SourceUnavailableError."""
        ...
