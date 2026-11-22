import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.errors import SourceUnavailableError
from app.schemas.job import Job, JobDetails, Source
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.retry import parse_retry_after
from app.utils.search_query import search_terms

logger = logging.getLogger(__name__)

# A digest reads every job before asking any page: the widest read (Remotar, 3 searches x 4 OR alternatives x
# 50 jobs = 600) must fit, or its first jobs are evicted and count as page failures. ~6 KB each at most.
_MAX_CACHED_DETAILS = 1000
_TIMEOUT_SECONDS = 10.0
_BLOCKED_STATUS = {403, 429}
JSON_HEADERS = {"User-Agent": "linkedin-api-job-alert/0.1", "Accept": "application/json"}


class JobSource(Protocol):
    """Where jobs come from. A new provider is a new class with this shape, added to the list in main.py.
    Every source is read on every cycle, all at the same time."""

    name: Source  # part of each search's baseline id, and the name in logs and in the source-down alert
    # True when fetch_details asks the network: the watcher then spaces that source's job pages.
    pages_need_requests: bool

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


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Listed:
    """One job of a search answer, as its source read it."""

    job: Job
    published: datetime
    described: bool  # has a description: a whole answer without one means the layout changed
    details: Callable[[], JobDetails]  # built only for a job that is kept
    wanted: bool = True  # False: recognized, but left out on purpose (a copy from another source, abroad)


class TermSearchSource(ABC):
    """A search that does not understand OR, NOT or quotes and already brings each job's description: each OR
    alternative is one request (page 1 only), spaced by `term_delay`; NOT is applied to the titles by the
    watcher. Requests carry no login and no cookies; 403 and 429 are a block."""

    name: Source
    pages_need_requests = False

    def __init__(
        self, client: httpx.AsyncClient, *, term_delay: float = 2.0, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self._client = client
        self._term_delay = term_delay
        self._clock = clock
        self._breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=300.0, max_recovery_timeout=3600.0, name=self.name
        )
        self._details = DetailsCache()

    @abstractmethod
    async def _fetch_term(self, term: str, window_seconds: int | None) -> list[object]:
        """The raw jobs of one term (use `_get`). Raises SourceUnavailableError."""

    @abstractmethod
    def _read(self, raw: object, now: datetime, location: str) -> Listed:
        """Raises ValueError (ValidationError is one) when the job is not recognized. `location` is JOB_LOCATION,
        for a source that filters the answer by it (`Listed.wanted`)."""

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        if not self._breaker.allow():
            raise SourceUnavailableError("circuit open", blocked=True)
        now = self._clock()
        since = None if window_seconds is None else now - timedelta(seconds=window_seconds)
        jobs: dict[str, Job] = {}
        for index, term in enumerate(search_terms(keywords)):
            if index:
                await asyncio.sleep(self._term_delay)
            raw_jobs = await self._fetch_term(term, window_seconds)
            recognized = described = 0
            for raw in raw_jobs:
                try:
                    listed = self._read(raw, now, location)
                except ValueError:
                    logger.debug("Skipped a %s job that failed validation (layout changed?)", self.name)
                    continue
                recognized += 1
                described += listed.described
                job = listed.job
                if not listed.wanted or (since is not None and listed.published < since) or job.key in jobs:
                    continue
                jobs[job.key] = job
                self._details.keep(job, listed.details())
            # A renamed field would otherwise pass for "no new jobs", or for jobs with no text, forever.
            if raw_jobs and not recognized:
                raise self._layout_changed("no job recognized in the answer")
            if recognized and not described:
                raise self._layout_changed("no job description in the answer")
        self._breaker.record_success()
        return list(jobs.values())

    async def fetch_details(self, job: Job) -> JobDetails:
        return self._details.get(job)

    async def _get(
        self, url: str, *, headers: Mapping[str, str], params: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """A 200 answer. Anything else feeds the breaker and raises SourceUnavailableError."""
        try:
            response = await self._client.get(url, params=params, headers=headers, timeout=_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            self._breaker.record_failure()
            raise SourceUnavailableError(f"network error: {type(e).__name__}") from e
        if response.status_code in _BLOCKED_STATUS:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            self._breaker.trip(retry_after)
            raise SourceUnavailableError(f"blocked ({response.status_code})", blocked=True, retry_after=retry_after)
        if response.status_code != 200:
            self._breaker.record_failure()
            raise SourceUnavailableError(f"status {response.status_code}")
        return response

    def _json_list(self, response: httpx.Response, key: str) -> list[object]:
        """The raw jobs under `key` of a JSON answer. Not JSON (a challenge page) or no list: layout changed."""
        try:
            data: object = response.json()
        except ValueError:
            data = None
        jobs = data.get(key) if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise self._layout_changed("no job list in the answer")
        return jobs

    def _layout_changed(self, what: str) -> SourceUnavailableError:
        """A failure, not "no new jobs": the answer no longer has what the parser expects."""
        self._breaker.record_failure()
        return SourceUnavailableError(f"{what}: layout changed")
