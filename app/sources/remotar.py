"""Remotar's public job search (remotar.com.br), a board of remote jobs in Brazil.

The JSON API behind its site, read without a key, login or cookies. It is not documented, so the fields can
change without notice. Automated reading carries the same kind of terms risk as the LinkedIn guest search.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.errors import SourceUnavailableError
from app.schemas.job import Job, JobDetails, Source
from app.sources.base import DetailsCache
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.html_text import html_to_text
from app.utils.retry import parse_retry_after
from app.utils.search_query import search_terms

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.remotar.com.br/jobs?search={term}"
_TIMEOUT_SECONDS = 10.0
_BLOCKED_STATUS = {403, 429}
_HEADERS = {"User-Agent": "linkedin-api-job-alert/0.1", "Accept": "application/json"}
# Only "remote" and "hybrid" were seen on 2026-11-18; "onsite" is a guess, and any other value adds no word.
_WORKPLACES = {"remote": "Remoto", "hybrid": "Híbrido", "onsite": "Presencial"}
# Jobs Remotar copies from Gupy: the Gupy source already reads them, so they would come twice under two keys.
_SKIPPED_INTEGRATION = "gupy"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Company(BaseModel):
    name: str | None = None


class _Requirement(BaseModel):
    description: str | None = None


class _Item(BaseModel):
    """One job of the search JSON, only the fields read here."""

    model_config = ConfigDict(alias_generator=to_camel)

    id: int  # a number in the JSON; Job checks its digits
    title: str
    description: str | None = None  # HTML, sometimes only a short summary
    job_requirements: list[_Requirement] = Field(default_factory=list)
    company: _Company | None = None
    company_display_name: str | None = None
    workplace_type: str | None = Field(default=None, alias="type")
    city: str | None = None
    state: str | None = None
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    expired: bool
    active: bool
    integration_source: str | None = None

    def job(self) -> Job:
        """Raises ValueError (ValidationError is one) when a field is invalid."""
        location = ", ".join(part for part in (self.city, self.state) if part)
        workplace = _WORKPLACES.get(self.workplace_type or "")
        if workplace:
            location = f"{location} ({workplace})" if location else workplace
        return Job(
            id=str(self.id),
            title=self.title,
            company=(self.company and self.company.name) or self.company_display_name or "",
            location=location,
            source="remotar",
        )

    @property
    def described(self) -> bool:
        return bool(html_to_text(self.description or ""))

    def details(self, now: datetime) -> JobDetails:
        # The requirements are a list apart from the description and weigh most in the score, so they go with it.
        # A job without any text is scored on its title (low, so dropped) instead of failing its page.
        requirements = [html_to_text(r.description or "") for r in self.job_requirements]
        text = html_to_text(self.description or "")
        if any(requirements):
            text = f"{text} Requisitos: {'; '.join(r for r in requirements if r)}"
        return JobDetails(
            description=text.strip() or self.title,
            closed=self.expired or not self.active or (self.expires_at is not None and self.expires_at < now),
        )


class _Page(BaseModel):
    data: list[object]


class RemotarSource:
    name: Source = "remotar"

    def __init__(
        self, client: httpx.AsyncClient, *, term_delay: float = 2.0, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self._client = client
        self._term_delay = term_delay
        self._clock = clock
        self._breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=300.0, max_recovery_timeout=3600.0, name="remotar"
        )
        self._details = DetailsCache()

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int | None) -> list[Job]:
        # `location` is not used: Remotar lists remote jobs in Brazil only.
        # Only page 1 (the newest 50) is read: results come newest first and the cycle runs every 30 minutes
        # (POLL_INTERVAL_SECONDS), so a term with more than 50 new jobs in that time is not expected.
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
                    logger.debug("Skipped a Remotar job that failed validation (layout changed?)")
                    continue
                recognized += 1
                described += item.described
                if item.integration_source == _SKIPPED_INTEGRATION:
                    continue
                if (since is not None and item.created_at < since) or job.key in jobs:
                    continue
                jobs[job.key] = job
                self._details.keep(job, item.details(now))
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

    async def _fetch_term(self, term: str) -> list[object]:
        url = SEARCH_URL.format(term=quote(term, safe=""))
        try:
            response = await self._client.get(url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
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
            return _Page.model_validate_json(response.content).data
        except ValueError:
            # Not JSON (a challenge page) or no job list: a failure, not "no new jobs".
            self._breaker.record_failure()
            raise SourceUnavailableError("no job list in the answer: layout changed") from None
