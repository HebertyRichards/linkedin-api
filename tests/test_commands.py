from datetime import UTC, datetime

from app.services.commands import HELP, Commands
from app.services.watcher import DigestResult
from tests.helpers import Clock


class FakeInbox:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def receive(self) -> list[str]:
        return []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class FakeRunner:
    def __init__(self, result: DigestResult | None) -> None:
        self.last_cycle_at: datetime | None = None
        self.next_digest_at = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
        self.result = result
        self.calls = 0

    async def digest_now(self) -> DigestResult | None:
        self.calls += 1
        return self.result


async def test_resumo_runs_the_digest_and_answers_how_many_went_out() -> None:
    inbox, runner = FakeInbox(), FakeRunner(DigestResult(sent=2, checked=20, pending=0))
    await Commands(inbox, runner).handle("/resumo@MeuBot")

    assert runner.calls == 1
    assert inbox.replies[-1] == "Resumo pronto: 2 vaga(s) com nota 50+ enviada(s)."


async def test_second_resumo_within_30_min_waits() -> None:
    clock = Clock()
    inbox, runner = FakeInbox(), FakeRunner(DigestResult(sent=0, checked=50, pending=0))
    commands = Commands(inbox, runner, clock=clock)

    await commands.handle("/resumo")
    assert "nenhuma vaga com nota 50+ entre as 50 olhadas" in inbox.replies[-1]
    clock.advance(minutes=10)
    await commands.handle("/resumo")

    assert runner.calls == 1
    assert inbox.replies[-1] == "O resumo rodou há pouco. Tente de novo em 20 min."


async def test_resumo_with_the_source_down_says_so() -> None:
    inbox = FakeInbox()
    await Commands(inbox, FakeRunner(None)).handle("/resumo")
    assert inbox.replies[-1].startswith("Não consegui rodar o resumo agora")


async def test_status_and_unknown_text() -> None:
    inbox = FakeInbox()
    commands = Commands(inbox, FakeRunner(None))

    await commands.handle("/start")
    await commands.handle("oi")

    assert "Próximo resumo automático: 25/10 18:00 UTC" in inbox.replies[0]
    assert "Último ciclo: ainda não rodou" in inbox.replies[0]
    assert inbox.replies[1] == HELP
