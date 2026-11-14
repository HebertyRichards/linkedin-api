from typing import Protocol

from app.schemas.job import Job, JobDetails


class JobSource(Protocol):
    """Where jobs come from. Swapping the provider is a new class with this shape, wired in main.py."""

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        """Easy Apply jobs posted in the last `window_seconds` for one search (None: any date).
        Raises SourceUnavailableError."""
        ...

    async def fetch_details(self, job: Job) -> JobDetails:
        """The job page: description and criteria. Raises SourceUnavailableError."""
        ...
