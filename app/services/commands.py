import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.errors import ChannelUnavailableError
from app.services.tailor import MIN_SCORE
from app.services.watcher import DigestResult

logger = logging.getLogger(__name__)

# Each /resumo spends up to 10 LLM scores and reads every search on LinkedIn: more often than this, the free
# quota runs out and LinkedIn starts answering 429.
MANUAL_DIGEST_COOLDOWN = timedelta(minutes=30)
# Pause after Telegram fails or another instance reads the bot (a deploy overlap), before reading again.
ERROR_PAUSE_SECONDS = 30.0

HELP = (
    "Comandos:\n"
    "/resumo - roda agora o resumo das vagas de qualquer data (só manda nota 50+)\n"
    "/status - mostra se o robô está ligado e quando sai o próximo resumo"
)


class Inbox(Protocol):
    async def receive(self) -> list[str]: ...

    async def reply(self, text: str) -> None: ...


class DigestRunner(Protocol):
    last_cycle_at: datetime | None

    @property
    def next_digest_at(self) -> datetime: ...

    async def digest_now(self) -> DigestResult | None: ...


def _when(moment: datetime) -> str:
    return moment.strftime("%d/%m %H:%M UTC")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest_answer(result: DigestResult | None) -> str:
    if result is None:
        return "Não consegui rodar o resumo agora (LinkedIn ou os canais fora do ar). Tente mais tarde."
    if result.sent:
        return f"Resumo pronto: {result.sent} vaga(s) com nota {MIN_SCORE}+ enviada(s)."
    if not result.checked:
        return "Resumo pronto: nenhuma vaga nova para avaliar."
    waiting = f", {result.pending} ainda sem nota" if result.pending else ""
    return f"Resumo pronto: nenhuma vaga com nota {MIN_SCORE}+ entre as {result.checked} olhadas{waiting}."


class Commands:
    """The bot's commands on Telegram: /resumo runs the digest now, /status says how the worker is."""

    def __init__(
        self,
        inbox: Inbox,
        watcher: DigestRunner,
        *,
        clock: Callable[[], datetime] = _utc_now,
        error_pause: float = ERROR_PAUSE_SECONDS,
    ) -> None:
        self._inbox = inbox
        self._watcher = watcher
        self._clock = clock
        self._error_pause = error_pause
        self._last_manual: datetime | None = None

    async def run(self) -> None:
        """Until cancelled. A failure never ends the worker: it pauses and reads again."""
        while True:
            try:
                for text in await self._inbox.receive():
                    await self.handle(text)
            except ChannelUnavailableError as e:
                logger.warning("Telegram commands: %s", e)
                await asyncio.sleep(self._error_pause)
            except Exception:
                logger.exception("Telegram commands failed")
                await asyncio.sleep(self._error_pause)

    async def handle(self, text: str) -> None:
        words = text.split()
        command = words[0].split("@")[0].lower() if words else ""  # "/resumo@MeuBot" in a group
        if command == "/resumo":
            await self._digest()
        elif command in ("/start", "/status"):
            await self._inbox.reply(self._status())
        else:
            await self._inbox.reply(HELP)

    async def _digest(self) -> None:
        now = self._clock()
        if self._last_manual is not None and now - self._last_manual < MANUAL_DIGEST_COOLDOWN:
            left = MANUAL_DIGEST_COOLDOWN - (now - self._last_manual)
            minutes = max(1, round(left / timedelta(minutes=1)))
            await self._inbox.reply(f"O resumo rodou há pouco. Tente de novo em {minutes} min.")
            return
        self._last_manual = now
        logger.info("Digest asked on Telegram")
        await self._inbox.reply("Rodando o resumo. Leva alguns minutos; as vagas chegam pelos canais de sempre.")
        await self._inbox.reply(_digest_answer(await self._watcher.digest_now()))

    def _status(self) -> str:
        last = self._watcher.last_cycle_at
        return (
            "Robô ligado.\n"
            f"Último ciclo: {_when(last) if last else 'ainda não rodou'}\n"
            f"Próximo resumo automático: {_when(self._watcher.next_digest_at)}\n\n{HELP}"
        )
