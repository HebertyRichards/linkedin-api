from datetime import UTC, datetime, timedelta

from app.channels.base import Message
from app.config.settings import Channel, Settings
from app.errors import SourceUnavailableError
from app.schemas.job import Job
from app.services.dispatcher import Dispatcher
from app.services.watcher import Watcher
from app.state.seen_store import SeenStore

WHATSAPP: dict[str, object] = {
    "evolution_api_url": "https://evolution.example.com",
    "evolution_api_key": "evo_test_key",
    "evolution_instance": "job-alerts",
    "whatsapp_to": "5511999999999",
}


def make_settings(**overrides: object) -> Settings:
    """Validated without reading the environment: model_validate skips the env and .env sources."""
    values: dict[str, object] = {
        "job_keywords": "python",
        "job_location": "Brazil",
        "resend_api_key": "re_test_key",
        "email_to": "me@example.com",
        **overrides,
    }
    return Settings.model_validate(values)


def job(n: int, title: str = "Python Developer") -> Job:
    return Job(id=str(4400000000 + n), title=f"{title} {n}", company="Acme", location="São Paulo, Brazil")


def card(job_id: str, title: str = "Python Developer", company: str = "Acme Corp") -> str:
    """A job card with the markup observed on the live search page (trimmed)."""
    return f"""
<li>
  <div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/x-{job_id}?trk=1">
      <span class="sr-only">{title}</span>
    </a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">
        {title}
      </h3>
      <h4 class="base-search-card__subtitle">
        <a class="hidden-nested-link" href="https://www.linkedin.com/company/acme">{company}</a>
      </h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">São Paulo, Brazil</span>
        <div class="job-posting-benefits"><span class="job-posting-benefits__text">Be an early applicant</span></div>
        <time class="job-search-card__listdate--new" datetime="2026-10-25">1 minute ago</time>
      </div>
    </div>
  </div>
</li>"""


class FakeSource:
    """Answers each fetch with the next scripted result."""

    def __init__(self, results: list[list[Job] | SourceUnavailableError]) -> None:
        self._results = results
        self.calls = 0

    async def fetch_recent(self, keywords: str, location: str) -> list[Job]:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        if isinstance(result, SourceUnavailableError):
            raise result
        return result


class FakeNotifier:
    def __init__(self, name: Channel, fail_with: Exception | None = None) -> None:
        self.name: Channel = name
        self.fail_with = fail_with
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


def make_watcher(
    source: FakeSource,
    channels: list[FakeNotifier],
    *,
    store: SeenStore | None = None,
    clock: Clock | None = None,
) -> Watcher:
    return Watcher(
        source=source,
        dispatcher=Dispatcher(channels),
        store=store or SeenStore(None),
        searches=["python"],
        location="Brazil",
        interval_seconds=60,
        search_spacing=(0.0, 0.0),
        clock=clock or Clock(),
    )
