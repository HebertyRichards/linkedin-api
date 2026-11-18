"""Workable's public job board (jobs.workable.com), the jobs of every company that hires through Workable.

It is not a documented API: it is the JSON the board's own search page reads, so it can change without notice.
Requests carry no login and no cookies on purpose. Automated reading carries the same kind of terms risk as the
LinkedIn guest search.
"""

import asyncio
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

SEARCH_URL = "https://jobs.workable.com/api/v1/jobs"
COUNTRY = "Brazil"
_TIMEOUT_SECONDS = 10.0
_BLOCKED_STATUS = {403, 429}
_HEADERS = {"User-Agent": "linkedin-api-job-alert/0.1", "Accept": "application/json"}
# Only a job page of the board: the code is all Job.url is built from, the slug after it is dropped.
_JOB_LINK = re.compile(r"https://jobs\.workable\.com/view/([A-Za-z0-9]{6,40})(?:/.*)?")
_WORKPLACES = {"remote": "Remoto", "hybrid": "Híbrido", "on_site": "Presencial"}
_ONE_DAY = timedelta(days=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Location(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    city: str | None = None
    subregion: str | None = None
    country_name: str


class _Company(BaseModel):
    title: str | None = None


class _Item(BaseModel):
    """One job of the search JSON, only the fields read here."""

    model_config = ConfigDict(alias_generator=to_camel)

    title: str
    url: str
    location: _Location
    company: _Company | None = None
    workplace: str | None = None
    employment_type: str | None = None
    created: AwareDatetime
    state: str
    description: str | None = None
    requirements_section: str | None = None

    def job(self) -> Job:
        """Raises ValueError (ValidationError is one) when a field is invalid or the link is not a job page."""
        link = _JOB_LINK.fullmatch(self.url)
        if link is None:
            raise ValueError("job link outside jobs.workable.com/view")
        location = ", ".join(part for part in (self.location.city, self.location.subregion) if part)
        workplace = _WORKPLACES.get(self.workplace or "")
        if workplace:
            location = f"{location} ({workplace})" if location else workplace
        return Job(
            id=link.group(1),
            title=self.title,
            company=self.company.title or "" if self.company else "",
            location=location,
            source="workable",
        )

    @property
    def described(self) -> bool:
        return bool(html_to_text(self.description or ""))

    def details(self) -> JobDetails:
        # Requirements go after the description: they weigh most in the score. A job without a description is
        # scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
        parts = (html_to_text(self.description or ""), html_to_text(self.requirements_section or ""))
        return JobDetails(
            description=" ".join(part for part in parts if part) if self.described else self.title,
            employment_type=self.employment_type or "",
            closed=self.state != "published",
        )


class WorkableSource:
    name: Source = "workable"

    def __init__(
        self, client: httpx.AsyncClient, *, term_delay: float = 2.0, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self._client = client
        self._term_delay = term_delay
        self._clock = clock
        self._breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=300.0, max_recovery_timeout=3600.0, name="workable"
        )
        self._details = DetailsCache()

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        # `location` is not used: the search is fixed to Brazil, like the other sources.
        # Results come by relevance and the board has no date sort (only a "posted in the last N days" filter,
        # probed on 2026-11-18), so a window up to a day asks for the last day's jobs and page 1 (20) of them is
        # read. A term with more than 20 jobs posted in a day may hide a new one past page 1: not expected here.
        if not self._breaker.allow():
            raise SourceUnavailableError("circuit open", blocked=True)
        now = self._clock()
        since = None if window_seconds is None else now - timedelta(seconds=window_seconds)
        last_day = since is not None and now - since <= _ONE_DAY
        jobs: dict[str, Job] = {}
        for index, term in enumerate(search_terms(keywords)):
            if index:
                await asyncio.sleep(self._term_delay)
            raw_jobs = await self._fetch_term(term, last_day=last_day)
            recognized = described = 0
            for raw in raw_jobs:
                try:
                    item = _Item.model_validate(raw)
                    job = item.job()
                except ValueError:
                    logger.debug("Skipped a Workable job that failed validation (layout changed?)")
                    continue
                recognized += 1
                described += item.described
                # The location param only ranks: none from elsewhere was seen, but nothing promises it.
                if item.location.country_name != COUNTRY:
                    continue
                if (since is not None and item.created < since) or job.key in jobs:
                    continue
                jobs[job.key] = job
                self._details.keep(job, item.details())
            # A renamed field would otherwise pass for "no new jobs", or for jobs with no text, forever.
            if raw_jobs and not recognized:
                self._breaker.record_failure()
                raise SourceUnavailableError("no job recognized in the answer: layout changed")
            if recognized and not described:
                self._breaker.record_failure()
                raise SourceUnavailableError("no job description in the answer: layout changed")
        self._breaker.record_success()
        return list(jobs.values())

    async def fetch_details(self, job: Job) -> JobDetails:
        return self._details.get(job)

    async def _fetch_term(self, term: str, *, last_day: bool) -> list[object]:
        params = {"query": term, "location": COUNTRY}
        if last_day:
            params["day_range"] = "1"
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
            data: object = response.json()
        except ValueError:
            data = None
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            self._breaker.record_failure()
            raise SourceUnavailableError("no job list in the answer: layout changed")
        return jobs
