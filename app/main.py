import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

import httpx

from app.channels.base import Notifier
from app.channels.email import EmailNotifier
from app.channels.whatsapp import WhatsAppNotifier
from app.config.logging_config import setup_logging
from app.config.settings import Settings, get_settings
from app.services.dispatcher import Dispatcher
from app.services.watcher import Watcher
from app.sources.linkedin_guest import LinkedInGuestSource
from app.state.seen_store import SeenStore

logger = logging.getLogger("app")


def build_channels(settings: Settings, client: httpx.AsyncClient) -> list[Notifier]:
    channels: list[Notifier] = []
    for name in settings.channels:
        channels.append(EmailNotifier(client, settings) if name == "email" else WhatsAppNotifier(client, settings))
    return channels


def _stop_on_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows' event loop has no signal handlers; Ctrl+C still cancels asyncio.run there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def run(settings: Settings) -> None:
    store = SeenStore(Path(settings.state_file) if settings.state_file else None)
    store.load()
    stop = asyncio.Event()
    _stop_on_signals(stop)

    # One client for the whole process: a new one per call would redo DNS + TCP + TLS every time.
    async with httpx.AsyncClient() as client:
        watcher = Watcher(
            source=LinkedInGuestSource(client),
            dispatcher=Dispatcher(build_channels(settings, client)),
            store=store,
            searches=settings.searches,
            location=settings.job_location,
            interval_seconds=settings.poll_interval_seconds,
        )
        logger.info(
            "Watching %d search(es) in '%s' every ~%ds; channels: %s; state: %s",
            len(settings.searches),
            settings.job_location,
            settings.poll_interval_seconds,
            " -> ".join(settings.channels),
            "file" if settings.state_file else "memory only",
        )
        try:
            await watcher.run(stop)
        finally:
            # SIGTERM lets the cycle in flight finish; the state is written once more before leaving.
            await store.save()
    logger.info("Stopped")


def main() -> None:
    try:
        settings = get_settings()
    except RuntimeError as e:
        sys.exit(str(e))
    setup_logging(settings.log_level, json_logs=settings.log_json or settings.is_production)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
