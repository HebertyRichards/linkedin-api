"""Gupy's public job search (portal.gupy.io), the biggest Brazilian ATS.

It is not an official API: there is none for search. This reads the public search page and the JSON that
Next.js embeds in it (`__NEXT_DATA__`), so the markup can change without notice. Requests carry no login and
no cookies on purpose. Automated reading carries the same kind of terms risk as the LinkedIn guest search.
"""

import html
import json
import re
from datetime import datetime
from urllib.parse import quote, urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.errors import SourceUnavailableError
from app.schemas.job import Job, JobDetails, Source
from app.sources.base import Listed, TermSearchSource

SEARCH_URL = "https://portal.gupy.io/job-search/term={term}"
_HEADERS = {"User-Agent": "linkedin-api-job-alert/0.1", "Accept": "text/html"}
_NEXT_DATA = re.compile(r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_JOB_LIST_PATH = ("props", "pageProps", "initialJobList", "data")
# Only a career page of a company on Gupy: one label before gupy.io, nothing else in the host.
_CAREER_HOST = re.compile(r"([a-z0-9-]+)\.gupy\.io")
_WORKPLACES = {"remote": "Remoto", "hybrid": "Híbrido", "on-site": "Presencial"}
_EMPLOYMENT = {"vacancy_type_effective": "Efetivo (CLT)", "vacancy_legal_entity": "Pessoa jurídica (PJ)"}


class _Item(BaseModel):
    """One job of the search JSON, only the fields read here."""

    model_config = ConfigDict(alias_generator=to_camel)

    id: int  # a number in the JSON; Job checks its digits
    name: str
    career_page_name: str | None = None
    city: str | None = None
    state: str | None = None
    workplace_type: str | None = None
    vacancy_type: str | None = Field(default=None, alias="type")
    published_date: AwareDatetime
    application_deadline: AwareDatetime | None = None
    job_url: str
    description: str | None = None

    def job(self) -> Job:
        """Raises ValueError (ValidationError is one) when a field is invalid or the link is not a career page."""
        link = urlsplit(self.job_url)
        host = _CAREER_HOST.fullmatch(link.netloc.lower())
        if link.scheme != "https" or host is None:
            raise ValueError("job link outside gupy.io")
        location = ", ".join(part for part in (self.city, self.state) if part)
        workplace = _WORKPLACES.get(self.workplace_type or "")
        if workplace:
            location = f"{location} ({workplace})" if location else workplace
        return Job(
            id=str(self.id),
            title=self.name,
            company=self.career_page_name or "",
            location=location,
            source="gupy",
            site=host.group(1),
        )

    @property
    def described(self) -> bool:
        return bool((self.description or "").strip())

    def details(self, now: datetime) -> JobDetails:
        # The description is plain text with HTML entities (&amp;, &nbsp;), as seen on 2026-11-17. A job without
        # one is scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
        description = html.unescape(self.description or "") if self.described else self.name
        return JobDetails(
            description=description,
            employment_type=_EMPLOYMENT.get(self.vacancy_type or "", ""),
            closed=self.application_deadline is not None and self.application_deadline < now,
        )


def parse_job_list(page: str) -> list[object]:
    """The raw jobs embedded in a search page. A page without them means Gupy changed the layout or answered
    with a challenge: that raises, so it counts as a source failure instead of passing for "no new jobs"."""
    match = _NEXT_DATA.search(page)
    if match is None:
        raise SourceUnavailableError("page without __NEXT_DATA__: layout changed or a challenge page")
    try:
        data: object = json.loads(match.group(1))
    except ValueError:
        raise SourceUnavailableError("unreadable __NEXT_DATA__: layout changed") from None
    for key in _JOB_LIST_PATH:
        if not isinstance(data, dict):
            break
        data = data.get(key)
    if not isinstance(data, list):
        raise SourceUnavailableError("no job list in __NEXT_DATA__: layout changed")
    return data


class GupyPortalSource(TermSearchSource):
    # `location` is not used: Gupy lists jobs in Brazil only. Only page 1 (the newest 12) is read: results come
    # newest first and the cycle asks for the last hour, so a term with more than 12 new jobs is not expected.
    name: Source = "gupy"

    async def _fetch_term(self, term: str, window_seconds: int | None) -> list[object]:
        response = await self._get(SEARCH_URL.format(term=quote(term, safe="")), headers=_HEADERS)
        try:
            return parse_job_list(response.text)
        except SourceUnavailableError:
            self._breaker.record_failure()
            raise

    def _read(self, raw: object, now: datetime) -> Listed:
        item = _Item.model_validate(raw)
        return Listed(item.job(), item.published_date, item.described, lambda: item.details(now))
