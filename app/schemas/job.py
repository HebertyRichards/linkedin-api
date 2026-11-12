from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TEXT = 200
# The description is cut so one assessment fits the LLM free-tier token budget (~1,500 tokens).
# Requirements usually come last, so a shorter cut loses what matters most.
MAX_DESCRIPTION = 6000
MAX_GAPS = 5
MAX_GAP = 80


class Job(BaseModel):
    """A job read from the source. Source data is untrusted, so every field is bounded here."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^\d{1,20}$")
    title: str = Field(min_length=1, max_length=MAX_TEXT)
    company: str = Field(default="", max_length=MAX_TEXT)
    location: str = Field(default="", max_length=MAX_TEXT)

    @field_validator("title", "company", "location", mode="before")
    @classmethod
    def _single_line(cls, v: object) -> object:
        # Collapses newlines and runs of spaces (they would break an email subject) and cuts long text.
        return " ".join(v.split())[:MAX_TEXT] if isinstance(v, str) else v

    @property
    def key(self) -> str:
        return f"linkedin:{self.id}"

    @property
    def url(self) -> str:
        # Built from the id, never copied from the page: a tampered link cannot reach the message.
        return f"https://www.linkedin.com/jobs/view/{self.id}/"


class JobDetails(BaseModel):
    """What the job page adds to the card. Read only to assess the job, never stored."""

    model_config = ConfigDict(frozen=True)

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION)
    seniority: str = Field(default="", max_length=MAX_TEXT)
    employment_type: str = Field(default="", max_length=MAX_TEXT)

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
