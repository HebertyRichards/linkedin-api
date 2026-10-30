import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from app.errors import SourceUnavailableError
from app.schemas.job import Job
from app.services.dispatcher import Dispatcher
from app.services.messages import batches, job_message, source_down_message
from app.sources.base import JobSource
from app.state.seen_store import SeenStore

logger = logging.getLogger(__name__)

SOURCE_DOWN_ALERT_AFTER = timedelta(minutes=10)
# The poll interval moves ±20% so requests do not land on the same second every time.
INTERVAL_JITTER = 0.2


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Watcher:
    """One cycle: read every search, keep the jobs not seen yet, deliver them, remember what went out."""

    def __init__(
        self,
        *,
        source: JobSource,
        dispatcher: Dispatcher,
        store: SeenStore,
        searches: Sequence[str],
        location: str,
        interval_seconds: int,
        search_spacing: tuple[float, float] = (2.0, 5.0),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._source = source
        self._dispatcher = dispatcher
        self._store = store
        self._searches = list(searches)
        self._location = location
        self._interval = interval_seconds
        self._search_spacing = search_spacing
        self._clock = clock
        self._failing_since: datetime | None = None
        self._down_alert_sent = False

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.cycle()
            except Exception:
                # A bug in one cycle must not end the worker: the next cycle starts clean.
                logger.exception("Cycle failed")
            delay = self._interval * random.uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)  # noqa: S311
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def cycle(self) -> None:
        now = self._clock()
        new: dict[str, Job] = {}
        failed = 0
        state_changed = False

        for index, keywords in enumerate(self._searches):
            if index:
                await asyncio.sleep(random.uniform(*self._search_spacing))  # noqa: S311
            try:
                jobs = await self._source.fetch_recent(keywords, self._location)
            except SourceUnavailableError as e:
                failed += 1
                logger.warning("Search '%s' failed: %s", keywords, e)
                continue

            search_id = f"{keywords}|{self._location}".lower()
            if not self._store.has_baseline(search_id):
                # First successful read of this search: what is already posted is not news.
                self._store.mark((job.key for job in jobs), now)
                self._store.add_baseline(search_id)
                state_changed = True
                logger.info("Baseline for '%s': %d current jobs marked as seen", keywords, len(jobs))
                continue
            for job in jobs:
                if not self._store.is_seen(job.key):
                    new.setdefault(job.key, job)

        await self._track_source_health(now, all_failed=failed == len(self._searches))
        if new:
            await self._notify(list(new.values()), now)
        if self._store.prune(now) or state_changed:
            await self._store.save()
        logger.info(
            "Cycle done: %d of %d search(es) answered, %d new job(s)",
            len(self._searches) - failed,
            len(self._searches),
            len(new),
        )

    async def _notify(self, jobs: list[Job], now: datetime) -> None:
        for group in batches(jobs):
            message = job_message(group)
            channel = await self._dispatcher.deliver(message)
            if channel is None:
                # Left unmarked on purpose: the next cycle tries these jobs again.
                return
            self._store.mark(message.keys, now)
            await self._store.save()
            logger.info("Notified %d job(s) via %s", len(group), channel)

    async def _track_source_health(self, now: datetime, *, all_failed: bool) -> None:
        if not all_failed:
            if self._failing_since is not None:
                logger.info("Job source recovered")
            self._failing_since = None
            self._down_alert_sent = False
            return
        if self._failing_since is None:
            self._failing_since = now
        if not self._down_alert_sent and now - self._failing_since >= SOURCE_DOWN_ALERT_AFTER:
            delivered = await self._dispatcher.deliver(source_down_message(self._failing_since))
            self._down_alert_sent = delivered is not None
