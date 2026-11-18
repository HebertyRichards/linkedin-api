"""Workable's public job board (jobs.workable.com), the jobs of every company that hires through Workable.

It is not a documented API: it is the JSON the board's own search page reads, so it can change without notice.
Requests carry no login and no cookies on purpose. Automated reading carries the same kind of terms risk as the
LinkedIn guest search.
"""

import re
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.job import Job, JobDetails, Source
from app.sources.base import JSON_HEADERS, Listed, TermSearchSource
from app.utils.html_text import html_to_text

SEARCH_URL = "https://jobs.workable.com/api/v1/jobs"
COUNTRY = "Brazil"
# Only a job page of the board: the code is all Job.url is built from, the slug after it is dropped.
_JOB_LINK = re.compile(r"https://jobs\.workable\.com/view/([A-Za-z0-9]{6,40})(?:/.*)?")
_WORKPLACES = {"remote": "Remoto", "hybrid": "Híbrido", "on_site": "Presencial"}
_ONE_DAY_SECONDS = 86_400


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

    def details(self, text: str) -> JobDetails:
        # Requirements go after the description: they weigh most in the score. A job without a description is
        # scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
        parts = (text, html_to_text(self.requirements_section or ""))
        return JobDetails(
            description=" ".join(part for part in parts if part) if text else self.title,
            employment_type=self.employment_type or "",
            closed=self.state != "published",
        )


class WorkableSource(TermSearchSource):
    # `location` is not used: the search is fixed to Brazil, like the other sources.
    # Results come by relevance and the board has no date sort (only a "posted in the last N days" filter,
    # probed on 2026-11-18), so a window up to a day asks for the last day's jobs and page 1 (20) of them is
    # read. A term with more than 20 jobs posted in a day may hide a new one past page 1: not expected here.
    name: Source = "workable"

    async def _fetch_term(self, term: str, window_seconds: int | None) -> list[object]:
        params = {"query": term, "location": COUNTRY}
        if window_seconds is not None and window_seconds <= _ONE_DAY_SECONDS:
            params["day_range"] = "1"
        response = await self._get(SEARCH_URL, params=params, headers=JSON_HEADERS)
        return self._json_list(response, "jobs")

    def _read(self, raw: object, now: datetime) -> Listed:
        item = _Item.model_validate(raw)
        text = html_to_text(item.description or "")
        # The location param only ranks: none from elsewhere was seen, but nothing promises it.
        return Listed(
            item.job(),
            item.created,
            bool(text),
            lambda: item.details(text),
            wanted=item.location.country_name == COUNTRY,
        )
