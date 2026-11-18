import base64
import json
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Source = Literal["linkedin", "gupy", "remotar", "himalayas", "workable"]
SOURCE_LABELS: dict[Source, str] = {
    "linkedin": "LinkedIn",
    "gupy": "Gupy",
    "remotar": "Remotar",
    "himalayas": "Himalayas",
    "workable": "Workable",
}
_DIGITS = re.compile(r"\d{1,20}")
# The id is all the link is built from, so each source's id has the exact shape of its link's variable part.
_ID_SHAPES: dict[Source, re.Pattern[str]] = {
    "linkedin": _DIGITS,
    "gupy": _DIGITS,
    "remotar": _DIGITS,
    "himalayas": re.compile(r"[a-z0-9-]{1,100}/[a-z0-9-]{1,100}"),  # company slug / job slug
    "workable": re.compile(r"[A-Za-z0-9]{6,40}"),  # the short code of jobs.workable.com/view/<code>
}

MAX_TEXT = 200
# The description is cut so one assessment fits the LLM free-tier token budget (~1,500 tokens).
# Requirements usually come last, so a shorter cut loses what matters most.
MAX_DESCRIPTION = 6000
MAX_GAPS = 5
MAX_GAP = 80


class Job(BaseModel):
    """A job read from the source. Source data is untrusted, so every field is bounded here."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=201)
    title: str = Field(min_length=1, max_length=MAX_TEXT)
    company: str = Field(default="", max_length=MAX_TEXT)
    location: str = Field(default="", max_length=MAX_TEXT)
    source: Source = "linkedin"
    site: str = Field(default="", pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)?$")

    @field_validator("title", "company", "location", mode="before")
    @classmethod
    def _single_line(cls, v: object) -> object:
        # Collapses newlines and runs of spaces (they would break an email subject) and cuts long text.
        return " ".join(v.split())[:MAX_TEXT] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _id_and_site_fit_the_source(self) -> Self:
        if not _ID_SHAPES[self.source].fullmatch(self.id):
            raise ValueError(f"id does not fit a {self.source} job")
        if (self.source == "gupy") != bool(self.site):
            raise ValueError("site is required for a Gupy job, and only for it")
        return self

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def url(self) -> str:
        # Built from the id, never copied from the page: a tampered link cannot reach the message.
        if self.source == "gupy":
            # Gupy's token is this JSON in URL-safe base64, as seen on its job links.
            payload = json.dumps({"jobId": int(self.id), "source": "gupy_portal"}, separators=(",", ":"))
            return f"https://{self.site}.gupy.io/job/{base64.urlsafe_b64encode(payload.encode()).decode()}"
        if self.source == "remotar":
            return f"https://remotar.com.br/job/{self.id}"
        if self.source == "himalayas":
            company, slug = self.id.split("/")
            return f"https://himalayas.app/companies/{company}/jobs/{slug}"
        if self.source == "workable":
            return f"https://jobs.workable.com/view/{self.id}"
        return f"https://www.linkedin.com/jobs/view/{self.id}/"


class JobDetails(BaseModel):
    """What the job page adds to the card. Read only to assess the job, never stored."""

    model_config = ConfigDict(frozen=True)

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION)
    seniority: str = Field(default="", max_length=MAX_TEXT)
    employment_type: str = Field(default="", max_length=MAX_TEXT)
    closed: bool = False

    @field_validator("description", mode="before")
    @classmethod
    def _cut(cls, v: object) -> object:
        return " ".join(v.split())[:MAX_DESCRIPTION] if isinstance(v, str) else v

    @field_validator("seniority", "employment_type", mode="before")
    @classmethod
    def _single_line(cls, v: object) -> object:
        return " ".join(v.split())[:MAX_TEXT] if isinstance(v, str) else v


Workplace = Literal["remote", "hybrid", "onsite", "unknown"]


class Assessment(BaseModel):
    """The LLM's verdict on one job. Its output is untrusted: bounded here like source data."""

    model_config = ConfigDict(frozen=True)

    score: int = Field(ge=0, le=100)
    workplace: Workplace
    reasons: str = Field(max_length=MAX_TEXT)
    gaps: tuple[str, ...] = Field(default=(), max_length=MAX_GAPS)

    @field_validator("reasons", mode="before")
    @classmethod
    def _single_line(cls, v: object) -> object:
        return clip(v, MAX_TEXT) if isinstance(v, str) else v

    @field_validator("gaps", mode="before")
    @classmethod
    def _short_items(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return tuple(clip(item, MAX_GAP) for item in v if isinstance(item, str) and item.strip())[:MAX_GAPS]


def clip(text: str, limit: int) -> str:
    """One line, cut at a word boundary with an ellipsis: the LLM does not always respect a length limit."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
