"""Remotar's public job search (remotar.com.br), a board of remote jobs in Brazil.

The JSON API behind its site, read without a key, login or cookies. It is not documented, so the fields can
change without notice. Automated reading carries the same kind of terms risk as the LinkedIn guest search.
"""

from datetime import datetime
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.job import Job, JobDetails, Source
from app.sources.base import JSON_HEADERS, Listed, TermSearchSource
from app.utils.html_text import html_to_text

SEARCH_URL = "https://api.remotar.com.br/jobs?search={term}"
# Only "remote" and "hybrid" were seen on 2026-11-18; "onsite" is a guess, and any other value adds no word.
_WORKPLACES = {"remote": "Remoto", "hybrid": "Híbrido", "onsite": "Presencial"}
# Jobs Remotar copies from Gupy: the Gupy source already reads them, so they would come twice under two keys.
_SKIPPED_INTEGRATION = "gupy"


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

    def details(self, text: str, now: datetime) -> JobDetails:
        # The requirements are a list apart from the description and weigh most in the score, so they go with it.
        # A job without any text is scored on its title (low, so dropped) instead of failing its page.
        requirements = [html_to_text(r.description or "") for r in self.job_requirements]
        if any(requirements):
            text = f"{text} Requisitos: {'; '.join(r for r in requirements if r)}"
        return JobDetails(
            description=text.strip() or self.title,
            closed=self.expired or not self.active or (self.expires_at is not None and self.expires_at < now),
        )


class RemotarSource(TermSearchSource):
    # `location` is not used: Remotar lists remote jobs in Brazil only. Only page 1 (the newest 50) is read:
    # results come newest first and the cycle asks for the last hour, so more than 50 new jobs is not expected.
    name: Source = "remotar"

    async def _fetch_term(self, term: str, window_seconds: int | None) -> list[object]:
        response = await self._get(SEARCH_URL.format(term=quote(term, safe="")), headers=JSON_HEADERS)
        return self._json_list(response, "data")

    def _read(self, raw: object, now: datetime, location: str) -> Listed:
        item = _Item.model_validate(raw)
        text = html_to_text(item.description or "")
        return Listed(
            item.job(),
            item.created_at,
            bool(text),
            lambda: item.details(text, now),
            wanted=item.integration_source != _SKIPPED_INTEGRATION,
        )
