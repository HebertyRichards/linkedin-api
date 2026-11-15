import asyncio
import contextlib
import logging
import signal
import sys
from datetime import timedelta
from pathlib import Path

import httpx

from app.channels.base import Notifier
from app.channels.email import EmailNotifier
from app.channels.telegram import TelegramInbox, TelegramNotifier
from app.channels.whatsapp import WhatsAppNotifier
from app.config.logging_config import setup_logging
from app.config.settings import Settings, get_settings
from app.schemas.resume import Contact, Resume, parse_contact, parse_resume, read_resume_file
from app.services.assessor import JobAssessor
from app.services.commands import Commands
from app.services.dispatcher import Dispatcher
from app.services.llm import build_chats
from app.services.tailor import ResumeTailor
from app.services.watcher import DIGEST_EVERY, INTERVAL_JITTER, Watcher
from app.sources.linkedin_guest import LinkedInGuestSource
from app.state.seen_store import SeenStore
from app.utils.watchdog import Watchdog

logger = logging.getLogger("app")


def build_channels(settings: Settings, client: httpx.AsyncClient) -> list[Notifier]:
    notifiers = {"email": EmailNotifier, "whatsapp": WhatsAppNotifier, "telegram": TelegramNotifier}
    return [notifiers[name](client, settings) for name in settings.channels]


def _stop_on_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows' event loop has no signal handlers; Ctrl+C still cancels asyncio.run there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def run(settings: Settings, profile: tuple[Resume, Contact]) -> None:
    store = SeenStore(Path(settings.state_file) if settings.state_file else None)
    store.load()
    stop = asyncio.Event()
    _stop_on_signals(stop)

    # One client for the whole process: a new one per call would redo DNS + TCP + TLS every time.
    async with httpx.AsyncClient() as client:
        assessor, tailor = _ai_services(client, settings, profile)
        watchdog = Watchdog(stall_after(settings.poll_interval_seconds))
        watcher = Watcher(
            source=LinkedInGuestSource(client),
            dispatcher=Dispatcher(build_channels(settings, client)),
            store=store,
            searches=settings.searches,
            location=settings.job_location,
            interval_seconds=settings.poll_interval_seconds,
            assessor=assessor,
            tailor=tailor,
            heartbeat=watchdog.beat,
        )
        logger.info(
            "Watching %d search(es) in '%s' every ~%ds, any date every %dh; channels: %s; state: %s; score: %s",
            len(settings.searches),
            settings.job_location,
            settings.poll_interval_seconds,
            DIGEST_EVERY // timedelta(hours=1),
            " -> ".join(settings.channels),
            "file" if settings.state_file else "memory only",
            f"{settings.llm_model} (resume: {settings.llm_tailor_model})"
            + (f", fallback {settings.llm_fallback_model}" if settings.llm_fallback_enabled else ""),
        )
        watchdog.start()
        command_task: asyncio.Task[None] | None = None
        if "telegram" in settings.channels:
            command_task = asyncio.create_task(Commands(TelegramInbox(client, settings), watcher).run())
        try:
            await watcher.run(stop)
        finally:
            if command_task is not None:
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
            watchdog.stop()
            # SIGTERM lets the cycle in flight finish; the state is written once more before leaving.
            await store.save()
    logger.info("Stopped")


def stall_after(interval_seconds: int) -> float:
    """How long the loop may go without a heartbeat. The longest normal silence is one sleep (the interval
    +20%) or one step: a cycle that scores and tailors 10 jobs, with the LLM waiting out its per-minute
    limit, takes up to ~50 min. One more hour of margin, so only a real stall restarts the worker."""
    return max(interval_seconds * (1 + INTERVAL_JITTER), 3600.0) + 3600.0


def _profile(settings: Settings) -> tuple[Resume, Contact]:
    text = settings.resume_yaml.get_secret_value() or read_resume_file(Path(settings.resume_file))
    return parse_resume(text), parse_contact(text)


def _ai_services(
    client: httpx.AsyncClient, settings: Settings, profile: tuple[Resume, Contact]
) -> tuple[JobAssessor, ResumeTailor]:
    resume, contact = profile
    score, tailor = build_chats(client, settings)
    return JobAssessor(score, resume), ResumeTailor(tailor, resume, contact)


def main() -> None:
    try:
        settings = get_settings()
        # Read before anything starts: a broken resume stops the boot instead of failing every job.
        profile = _profile(settings)
    except RuntimeError as e:
        sys.exit(str(e))
    setup_logging(settings.log_level, json_logs=settings.log_json or settings.is_production)
    asyncio.run(run(settings, profile))


if __name__ == "__main__":
    main()
