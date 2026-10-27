from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TEXT = 200


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
