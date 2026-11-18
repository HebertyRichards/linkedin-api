from typing import Protocol

from app.errors import SourceUnavailableError
from app.schemas.job import Job, JobDetails, Source

_MAX_CACHED_DETAILS = 500


class JobSource(Protocol):
    """Where jobs come from. A new provider is a new class with this shape, added to the list in main.py.
    Every source is read on every cycle, all at the same time."""

    name: Source  # part of each search's baseline id, and the name in logs and in the source-down alert

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        """Jobs posted in the last `window_seconds` for one search (None: any date).
        Raises SourceUnavailableError."""
        ...

    async def fetch_details(self, job: Job) -> JobDetails:
        """The job page: description and criteria. Raises SourceUnavailableError."""
        ...


class DetailsCache:
    """For a source whose search already has each job's description: the details are kept here instead of a
    second request per job, so fetch_details never touches the network."""

    def __init__(self) -> None:
        self._details: dict[str, JobDetails] = {}

    def keep(self, job: Job, details: JobDetails) -> None:
        self._details.pop(job.key, None)
        self._details[job.key] = details
        if len(self._details) > _MAX_CACHED_DETAILS:
            del self._details[next(iter(self._details))]

    def get(self, job: Job) -> JobDetails:
        """Raises SourceUnavailableError for a job not seen by a recent search."""
        details = self._details.get(job.key)
        if details is None:
            raise SourceUnavailableError("no cached details for this job")
        return details
