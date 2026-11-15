import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.errors import ChannelUnavailableError
from app.services.tailor import MIN_SCORE
from app.services.watcher import LIST_SIZE, DigestResult, ListResult

logger = logging.getLogger(__name__)

# Each /resumo spends up to 50 LLM scores (MAX_DIGEST_ROUNDS rounds of 10) and /vagas up to 20, and both read
# every search on LinkedIn: more often than this, the free quota runs out and LinkedIn starts answering 429.
# Each command has its own clock.
MANUAL_COOLDOWN = timedelta(minutes=30)
# Pause after Telegram fails or another instance reads the bot (a deploy overlap), before reading again.
ERROR_PAUSE_SECONDS = 30.0

HELP = (
    "Comandos:\n"
    "/resumo - roda agora o resumo das vagas de qualquer data (só manda nota 50+)\n"
    f"/vagas - lista {LIST_SIZE} vagas de qualquer data com a nota de cada uma, qualquer nota\n"
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

    async def list_now(self) -> ListResult | None: ...


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


def _list_answer(result: ListResult | None) -> str:
    if result is None:
        return "Não consegui listar as vagas agora (LinkedIn ou os canais fora do ar). Tente mais tarde."
    if not result.shown:
        return "Nenhuma vaga encontrada nas buscas."
    unscored = result.shown - result.scored
    waiting = f", {unscored} sem nota (IA ou LinkedIn fora)" if unscored else ""
    return f"Lista pronta: {result.shown} vaga(s){waiting}."


class Commands:
    """The bot's commands on Telegram: /resumo runs the digest now, /vagas lists jobs with any score,
    /status says how the worker is."""

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
        self._last_run: dict[str, datetime] = {}  # per command: /resumo does not hold /vagas

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
        elif command == "/vagas":
            await self._list()
        elif command in ("/start", "/status"):
            await self._inbox.reply(self._status())
        else:
            await self._inbox.reply(HELP)

    async def _cooling(self, command: str, what: str) -> bool:
        """True (and says so) when the command ran less than MANUAL_COOLDOWN ago; else starts its clock."""
        now = self._clock()
        last = self._last_run.get(command)
        if last is not None and now - last < MANUAL_COOLDOWN:
            minutes = max(1, round((MANUAL_COOLDOWN - (now - last)) / timedelta(minutes=1)))
            await self._inbox.reply(f"{what} rodou há pouco. Tente de novo em {minutes} min.")
            return True
        self._last_run[command] = now
        return False

    async def _list(self) -> None:
        if await self._cooling("/vagas", "A lista"):
            return
        logger.info("Job list asked on Telegram")
        await self._inbox.reply(
            f"Avaliando {LIST_SIZE} vagas. Leva alguns minutos; a lista chega pelos canais de sempre."
        )
        await self._inbox.reply(_list_answer(await self._watcher.list_now()))

    async def _digest(self) -> None:
        if await self._cooling("/resumo", "O resumo"):
            return
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
