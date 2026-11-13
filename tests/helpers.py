from datetime import UTC, datetime, timedelta

from app.channels.base import Attachment, Message
from app.config.settings import Channel, Settings
from app.errors import SourceUnavailableError
from app.schemas.job import Assessment, Job, JobDetails, Workplace
from app.schemas.resume import TailoredResume
from app.services.dispatcher import Dispatcher
from app.services.tailor import TailoredFile
from app.services.watcher import Watcher
from app.state.seen_store import SeenStore

WHATSAPP: dict[str, object] = {
    "evolution_api_url": "https://evolution.example.com",
    "evolution_api_key": "evo_test_key",
    "evolution_instance": "job-alerts",
    "whatsapp_to": "5511999999999",
}
# Made-up token in the BotFather format.
TELEGRAM_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghij"  # secrets-ok gitleaks:allow
TELEGRAM: dict[str, object] = {"telegram_bot_token": TELEGRAM_TOKEN, "telegram_chat_id": "987654321"}


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


# A made-up resume (nobody's real data) with a contact block, to prove the contact never reaches the prompt.
RESUME_YAML = """
contato: {nome: Ana Exemplo, telefone: "(00) 00000-0000", email: ana@example.com}
titulo: Engenheira de Dados
idiomas: [Espanhol (B1)]
perfil_busca: {niveis: [Estágio], modelos_trabalho: [Remoto]}
resumo: Engenheira de dados com foco em pipelines.
experiencias:
  - id: padaria
    empresa: Padaria Modelo
    cargo: Estagiária de Dados
    inicio: 2024-03
    tecnologias: [Go, Kafka]
    destaques:
      - {id: padaria-etl, texto: Pipeline de vendas diárias em Go., skills: [Go]}
      - {id: padaria-fila, texto: Fila de pedidos com Kafka., skills: [Kafka]}
projetos:
  - id: clima
    nome: Painel do Clima
    fixo: true
    tecnologias: [Rust]
    destaques: [{id: clima-api, texto: Coleta de dados meteorológicos.}]
  - id: feira
    nome: Mapa da Feira
    tecnologias: [Go]
    destaques: [{id: feira-mapa, texto: Mapa das barracas da feira.}]
  - id: horta
    nome: Diário da Horta
    tecnologias: [Kafka]
    destaques: [{id: horta-log, texto: Registro de regas.}]
formacao: [{curso: Estatística, instituicao: Universidade Exemplo, periodo: 2022 - 2026, status: Cursando}]
habilidades: {dados: [Go, Kafka, Rust]}
habilidades_sob_demanda: [Airflow]
"""

DETAILS = JobDetails(
    description="Buscamos pessoa de dados com Go.", seniority="Internship", employment_type="Full-time"
)


def assessment(score: int, workplace: Workplace = "remote") -> Assessment:
    return Assessment(score=score, workplace=workplace, reasons="Stack parecida.", gaps=["Kubernetes"])


class FakeSource:
    """Answers each fetch with the next scripted result; job pages with `details`."""

    def __init__(
        self,
        results: list[list[Job] | SourceUnavailableError],
        details: JobDetails | SourceUnavailableError = DETAILS,
    ) -> None:
        self._results = results
        self._details = details
        self.calls = 0
        self.windows: list[int] = []
        self.detail_calls: list[str] = []

    async def fetch_details(self, job: Job) -> JobDetails:
        self.detail_calls.append(job.key)
        if isinstance(self._details, SourceUnavailableError):
            raise self._details
        return self._details

    async def fetch_recent(self, keywords: str, location: str, *, window_seconds: int) -> list[Job]:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        self.windows.append(window_seconds)
        if isinstance(result, SourceUnavailableError):
            raise result
        return result


class FakeAssessor:
    """Scores from a table by job key; a key missing from the table is a job that could not be scored."""

    def __init__(self, scores: dict[str, Assessment], cache: dict[str, Assessment] | None = None) -> None:
        self._scores = scores
        self._cache = cache or {}

    def cached(self, job: Job) -> Assessment | None:
        return self._cache.get(job.key)

    async def assess(self, job: Job, details: JobDetails) -> Assessment | None:
        return self._scores.get(job.key)


class FakeTailor:
    """Builds a small fake file for any job and records which jobs asked for one."""

    def __init__(self, *, down: bool = False) -> None:
        self.calls: list[str] = []
        self._down = down  # the LLM did not answer: no file

    def cached(self, job: Job) -> TailoredFile | None:
        return None

    async def tailor(self, job: Job, details: JobDetails) -> TailoredFile | None:
        self.calls.append(job.key)
        if self._down:
            return None
        resume = TailoredResume(resumo="Resumo ajustado.", experiencias=[], projetos=[], habilidades=["Go"])
        attachment = Attachment(f"Curriculo-{job.id}.pdf", b"%PDF-fake", f"Currículo para: {job.title}")
        return TailoredFile(resume=resume, attachment=attachment)


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
    searches: list[str] | None = None,
    assessor: FakeAssessor | None = None,
    tailor: FakeTailor | None = None,
) -> Watcher:
    return Watcher(
        source=source,
        dispatcher=Dispatcher(channels),
        store=store or SeenStore(None),
        searches=searches or ["python"],
        location="Brazil",
        interval_seconds=60,
        search_spacing=(0.0, 0.0),
        job_page_spacing=(0.0, 0.0),
        clock=clock or Clock(),
        assessor=assessor,
        tailor=tailor,
    )
