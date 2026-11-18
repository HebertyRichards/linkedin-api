from datetime import timedelta
from typing import Protocol

from app.schemas.job import Job, JobDetails, Source


class JobSource(Protocol):
    """Where jobs come from. A new provider is a new class with this shape, added to the list in main.py."""

    name: Source  # part of each search's baseline id, and the name in logs and in the source-down alert
    # Least time between two reads by the cycle (None: every cycle). The digest and /vagas read it every time.
    min_interval: timedelta | None

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        """Jobs posted in the last `window_seconds` for one search (None: any date).
        Raises SourceUnavailableError."""
        ...

    async def fetch_details(self, job: Job) -> JobDetails:
        """The job page: description and criteria. Raises SourceUnavailableError."""
        ...
