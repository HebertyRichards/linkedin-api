"""Himalayas' public job search API (himalayas.app), a board of remote jobs only.

It is documented and needs no key and no login; requests carry no cookies. It has a documented rate limit,
so a 429 is treated as a block.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.errors import SourceUnavailableError
from app.schemas.job import Job, JobDetails, Source
from app.sources.base import DetailsCache
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.html_text import html_to_text
from app.utils.retry import parse_retry_after
from app.utils.search_query import search_terms

logger = logging.getLogger(__name__)

SEARCH_URL = "https://himalayas.app/jobs/api/search"
_TIMEOUT_SECONDS = 10.0
_BLOCKED_STATUS = {403, 429}
_HEADERS = {"User-Agent": "linkedin-api-job-alert/0.1", "Accept": "application/json"}
# The job page and nothing else: the id is rebuilt from these two slugs, so any other link is rejected.
_GUID = re.compile(r"https://himalayas\.app/companies/([a-z0-9-]{1,100})/jobs/([a-z0-9-]{1,100})")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Item(BaseModel):
    """One job of the search JSON, only the fields read here."""

    model_config = ConfigDict(alias_generator=to_camel)

    title: str
    company_name: str | None = None
    employment_type: str | None = None
    seniority: list[str] = []
    location_restrictions: list[str] = []
    description: str | None = None
    pub_date: AwareDatetime  # unix seconds in the JSON, read as UTC
    expiry_date: AwareDatetime | None = None
    guid: str

    def job(self) -> Job:
        """Raises ValueError (ValidationError is one) when a field is invalid or the guid is not a job page."""
        match = _GUID.fullmatch(self.guid)
        if match is None:
            raise ValueError("guid is not a himalayas.app job page")
        location = "Remoto"  # every Himalayas job is remote
        if self.location_restrictions:
            location = f"{location} ({', '.join(self.location_restrictions)})"
        return Job(
            id=f"{match.group(1)}/{match.group(2)}",
            title=self.title,
            company=self.company_name or "",
            location=location,
            source="himalayas",
        )

    @property
    def text(self) -> str:
        return html_to_text(self.description or "")

    def details(self, now: datetime) -> JobDetails:
        # A job without a description is scored on its title (low, so dropped), as in the Gupy source.
        return JobDetails(
            description=self.text or self.title,
            seniority=", ".join(self.seniority),
            employment_type=self.employment_type or "",
            closed=self.expiry_date is not None and self.expiry_date < now,
        )


def parse_job_list(body: str) -> list[object]:
    """The raw jobs of a search answer. An answer without them means the API changed: that raises, so it
    counts as a source failure instead of passing for "no new jobs"."""
    try:
        data: object = json.loads(body)
    except ValueError:
        raise SourceUnavailableError("unreadable JSON: API changed") from None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise SourceUnavailableError("no job list in the answer: API changed")
    return jobs


class HimalayasSource:
    name: Source = "himalayas"

    def __init__(
        self, client: httpx.AsyncClient, *, term_delay: float = 2.0, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self._client = client
        self._term_delay = term_delay
        self._clock = clock
        self._breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=300.0, max_recovery_timeout=3600.0, name="himalayas"
        )
        self._details = DetailsCache()

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        # `location` is not used: the searches are for Brazil, so country=BR is fixed in the request.
        # Only page 1 (20 jobs) is read: sort=recent puts the newest first (mostly by date, as seen on
        # 2026-11-18) and the cycle runs every 30 minutes, so more than 20 new jobs per term is not expected.
        if not self._breaker.allow():
            raise SourceUnavailableError("circuit open", blocked=True)
        now = self._clock()
        since = None if window_seconds is None else now - timedelta(seconds=window_seconds)
        jobs: dict[str, Job] = {}
        for index, term in enumerate(search_terms(keywords)):
            if index:
                await asyncio.sleep(self._term_delay)
            raw_jobs = await self._fetch_term(term)
            recognized = described = 0
            for raw in raw_jobs:
                try:
                    item = _Item.model_validate(raw)
                    job = item.job()
                except ValueError:
                    logger.debug("Skipped a Himalayas job that failed validation (API changed?)")
                    continue
                recognized += 1
                described += bool(item.text)
                if (since is not None and item.pub_date < since) or job.key in jobs:
                    continue
                jobs[job.key] = job
                self._details.keep(job, item.details(now))
            # A renamed field would otherwise pass for "no new jobs", or for jobs with no text, forever.
            if raw_jobs and not recognized:
                self._breaker.record_failure()
                raise SourceUnavailableError("no job recognized in the answer: API changed")
            if recognized and not described:
                self._breaker.record_failure()
                raise SourceUnavailableError("no job description in the answer: API changed")
        self._breaker.record_success()
        return list(jobs.values())

    async def fetch_details(self, job: Job) -> JobDetails:
        return self._details.get(job)

    async def _fetch_term(self, term: str) -> list[object]:
        params = {"q": term, "country": "BR", "sort": "recent"}
        try:
            response = await self._client.get(SEARCH_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
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
        try:
            return parse_job_list(response.text)
        except SourceUnavailableError:
            self._breaker.record_failure()
            raise
