"""Himalayas' public job search API (himalayas.app), a board of remote jobs only.

It is documented and needs no key and no login; requests carry no cookies. It has a documented rate limit,
so a 429 is treated as a block.
"""

import re
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.job import Job, JobDetails, Source
from app.sources.base import JSON_HEADERS, Listed, TermSearchSource
from app.utils.html_text import html_to_text

SEARCH_URL = "https://himalayas.app/jobs/api/search"
# The job page and nothing else: the id is rebuilt from these two slugs, so any other link is rejected.
_GUID = re.compile(r"https://himalayas\.app/companies/([a-z0-9-]{1,100})/jobs/([a-z0-9-]{1,100})")


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

    def details(self, text: str, now: datetime) -> JobDetails:
        # A job without a description is scored on its title (low, so dropped), as in the Gupy source.
        return JobDetails(
            description=text or self.title,
            seniority=", ".join(self.seniority),
            employment_type=self.employment_type or "",
            closed=self.expiry_date is not None and self.expiry_date < now,
        )


class HimalayasSource(TermSearchSource):
    # `location` is not used: the searches are for Brazil, so country=BR is fixed in the request. Only page 1
    # (20 jobs) is read: sort=recent puts the newest first (mostly by date, as seen on 2026-11-18) and the cycle
    # asks for the last hour, so more than 20 new jobs per term is not expected.
    name: Source = "himalayas"

    async def _fetch_term(self, term: str, window_seconds: int | None) -> list[object]:
        params = {"q": term, "country": "BR", "sort": "recent"}
        response = await self._get(SEARCH_URL, params=params, headers=JSON_HEADERS)
        return self._json_list(response, "jobs")

    def _read(self, raw: object, now: datetime) -> Listed:
        item = _Item.model_validate(raw)
        text = html_to_text(item.description or "")
        return Listed(item.job(), item.pub_date, bool(text), lambda: item.details(text, now))
