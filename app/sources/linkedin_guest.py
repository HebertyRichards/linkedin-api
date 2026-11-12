"""LinkedIn's public (logged-out) job search, the endpoint behind linkedin.com/jobs for visitors.

It is not an official API: LinkedIn's terms forbid automated access, and the markup can change without
notice. Requests carry no login and no cookies on purpose. See SCOPE.md, section 1.2.
"""

import asyncio
import logging
from html.parser import HTMLParser

import httpx
from pydantic import ValidationError

from app.errors import SourceUnavailableError
from app.schemas.job import Job, JobDetails
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.retry import parse_retry_after

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
PAGE_SIZE = 10
MAX_PAGES = 3
_TIMEOUT_SECONDS = 10.0
_BLOCKED_STATUS = {429, 999}
_URN_PREFIX = "urn:li:jobPosting:"
_HEADERS = {"User-Agent": "linkedin-api-job-alert/0.1", "Accept": "text/html"}
# CSS class on a card element -> Job field, as observed on the live page.
_FIELDS = {
    "base-search-card__title": "title",
    "base-search-card__subtitle": "company",
    "job-search-card__location": "location",
}
_FIELD_TAGS = frozenset({"h3", "h4", "span"})
# Job page, as observed on the live page (2026-11-12): the description and the criteria list.
_DESCRIPTION_CLASS = "show-more-less-html__markup"
_CRITERIA_LABEL_CLASS = "description__job-criteria-subheader"
_CRITERIA_VALUE_CLASS = "description__job-criteria-text"
_CRITERIA = {"Seniority level": "seniority", "Employment type": "employment_type"}
# Block tags end a line, so words of two paragraphs do not run together.
_BLOCK_TAGS = frozenset({"p", "li", "br", "div", "ul", "ol", "h1", "h2", "h3", "h4"})


class _CardParser(HTMLParser):
    """Collects the job cards of a search page, reading only the fields the alert needs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self.tags = 0
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags += 1
        attributes = dict(attrs)
        urn = attributes.get("data-entity-urn") or ""
        if urn.startswith(_URN_PREFIX):
            self.cards.append({"id": urn.removeprefix(_URN_PREFIX)})
            self._field = None
            return
        if not self.cards or tag not in _FIELD_TAGS:
            return
        for css_class in (attributes.get("class") or "").split():
            if css_class in _FIELDS:
                self._field = _FIELDS[css_class]
                return

    def handle_endtag(self, tag: str) -> None:
        if tag in _FIELD_TAGS:
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            card = self.cards[-1]
            card[self._field] = f"{card.get(self._field, '')} {data}"


class _DetailParser(HTMLParser):
    """Collects the description text and the criteria (seniority, employment type) of a job page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description: list[str] = []
        self.criteria: dict[str, str] = {}
        self._description_depth = 0  # >0 while inside the description element
        self._label: str | None = None
        self._capture: str | None = None  # "label" or "value" while inside a criteria element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if self._description_depth:
            if tag == "div":
                self._description_depth += 1
            if tag in _BLOCK_TAGS:
                self.description.append("\n")
        elif tag == "div" and _DESCRIPTION_CLASS in classes:
            self._description_depth = 1
        elif _CRITERIA_LABEL_CLASS in classes:
            self._capture, self._label = "label", ""
        elif _CRITERIA_VALUE_CLASS in classes and self._label is not None:
            self._capture = "value"

    def handle_endtag(self, tag: str) -> None:
        if self._description_depth and tag == "div":
            self._description_depth -= 1
        if self._capture == "label" and tag == "h3":
            self._capture = None
        elif self._capture == "value" and tag == "span":
            self._capture, self._label = None, None

    def handle_data(self, data: str) -> None:
        if self._description_depth:
            self.description.append(data)
        elif self._capture == "label" and self._label is not None:
            self._label += data
        elif self._capture == "value" and self._label is not None:
            field = _CRITERIA.get(" ".join(self._label.split()))
            if field:
                self.criteria[field] = self.criteria.get(field, "") + data


def parse_details(html: str) -> JobDetails:
    """Raises when the page has no description: LinkedIn changed the markup or answered with a challenge."""
    parser = _DetailParser()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.description).splitlines())
    try:
        return JobDetails(description="\n".join(line for line in lines if line), **parser.criteria)
    except ValidationError:
        raise SourceUnavailableError("job page without a description: markup changed or a challenge page") from None


def parse_cards(html: str) -> tuple[list[Job], int]:
    """The valid jobs on a page and how many cards it had (a short page means it was the last).

    A search with no results comes back with no tags at all ("<!DOCTYPE html><!---->"). Markup without a
    usable card means LinkedIn changed the page or answered with a challenge: that raises, so it counts as
    a source failure instead of passing for "no new jobs".
    """
    parser = _CardParser()
    parser.feed(html)
    parser.close()
    if not parser.cards:
        if parser.tags:
            raise SourceUnavailableError("unrecognized page: markup changed or a challenge page")
        return [], 0
    jobs: list[Job] = []
    for card in parser.cards:
        try:
            jobs.append(
                Job(
                    id=card.get("id", ""),
                    title=card.get("title", ""),
                    company=card.get("company", ""),
                    location=card.get("location", ""),
                )
            )
        except ValidationError:
            logger.warning("Skipped a job card that failed validation (markup changed?)")
    if not jobs:
        raise SourceUnavailableError(f"none of the {len(parser.cards)} job cards passed validation: markup changed")
    return jobs, len(parser.cards)


class LinkedInGuestSource:
    def __init__(self, client: httpx.AsyncClient, *, page_delay: float = 1.5) -> None:
        self._client = client
        self._page_delay = page_delay
        self._breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=300.0, max_recovery_timeout=3600.0, name="linkedin"
        )

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int) -> list[Job]:
        # Results are not sorted by date, so the whole window is read instead of only the first page.
        if not self._breaker.allow():
            raise SourceUnavailableError("circuit open", blocked=True)
        jobs: list[Job] = []
        for page in range(MAX_PAGES):
            if page:
                await asyncio.sleep(self._page_delay)
            page_jobs, cards = await self._fetch_page(keywords, location, window_seconds, start=page * PAGE_SIZE)
            jobs.extend(page_jobs)
            if cards < PAGE_SIZE:
                break
        self._breaker.record_success()
        return jobs

    async def fetch_details(self, job: Job) -> JobDetails:
        """One job page. Only a block answer touches the breaker: a page that fails to parse must not
        stop the search itself. An open circuit counts as a block, so the caller holds the job."""
        if not self._breaker.allow():
            raise SourceUnavailableError("circuit open", blocked=True)
        try:
            response = await self._client.get(DETAIL_URL.format(id=job.id), headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise SourceUnavailableError(f"network error: {type(e).__name__}") from e
        if response.status_code in _BLOCKED_STATUS:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            self._breaker.trip(retry_after)
            raise SourceUnavailableError(f"blocked ({response.status_code})", blocked=True, retry_after=retry_after)
        if response.status_code != 200:
            raise SourceUnavailableError(f"status {response.status_code}")
        return parse_details(response.text)

    async def _fetch_page(
        self, keywords: str, location: str, window_seconds: int, *, start: int
    ) -> tuple[list[Job], int]:
        params = {
            "keywords": keywords,
            "location": location,
            "f_AL": "true",
            "f_TPR": f"r{window_seconds}",
            "start": str(start),
        }
        try:
            response = await self._client.get(SEARCH_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            self._breaker.record_failure()
            raise SourceUnavailableError(f"network error: {type(e).__name__}") from e

        if response.status_code in _BLOCKED_STATUS:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            self._breaker.trip(retry_after)
            raise SourceUnavailableError(f"blocked ({response.status_code})", blocked=True, retry_after=retry_after)
        if start and response.status_code == 400:
            # Asking past the last page.
            return [], 0
        if response.status_code != 200:
            self._breaker.record_failure()
            raise SourceUnavailableError(f"status {response.status_code}")
        try:
            return parse_cards(response.text)
        except SourceUnavailableError:
            self._breaker.record_failure()
            raise
