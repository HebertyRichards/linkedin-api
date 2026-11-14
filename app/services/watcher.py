import asyncio
import contextlib
import logging
import random
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.errors import SourceUnavailableError
from app.schemas.job import Assessment, Job, JobDetails
from app.services.assessor import Assessor
from app.services.dispatcher import Dispatcher
from app.services.messages import MAX_PER_MESSAGE, batches, job_message, source_down_message
from app.services.tailor import TAILOR_MIN_SCORE, Tailor, TailoredFile
from app.sources.base import JobSource
from app.state.seen_store import SeenStore

logger = logging.getLogger(__name__)

# LinkedIn's 429s on a datacenter IP usually clear within 30 min, and the digest catches up on what was missed.
SOURCE_DOWN_ALERT_AFTER = timedelta(hours=1)
# The poll interval moves ±20% so requests do not land on the same second every time.
INTERVAL_JITTER = 0.2
# The cycle reads the last hour, wider than the poll interval on purpose: a job can reach the search a
# while after its posting time, and the seen-set already drops the repeats.
CYCLE_WINDOW_SECONDS = 3600
# The digest reads the last 3 days, never older: it catches jobs that reached the search after the cycle's window.
# Every 4 hours: each digest scores up to 10 jobs, and the LLM free quota has to last the day.
DIGEST_EVERY = timedelta(hours=4)
DIGEST_WINDOW_SECONDS = 3 * 86400
# Bound for the held-job keys; past it they are forgotten (worst case a job is held one more time).
_HELD_LIMIT = 1000


_NOT = re.compile(r'\bNOT\s+(?:"([^"]+)"|(\S+))')


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def excluded_terms(keywords: str) -> list[str]:
    """The `NOT term` and `NOT "two words"` of a search, without accents."""
    return [_plain(phrase or word) for phrase, word in _NOT.findall(keywords)]


def is_excluded(title: str, terms: Sequence[str]) -> bool:
    """LinkedIn's guest search does not always honor NOT, and matches "consultor" exactly where
    "Consultores" is the same role: a title with a word starting with an excluded term is dropped here."""
    plain = _plain(title)
    return any(re.search(rf"\b{re.escape(term)}", plain) for term in terms)


@dataclass
class _Batch:
    """Jobs to send, best score first, with what was found for them."""

    jobs: list[Job]
    scores: dict[str, Assessment] = field(default_factory=dict)
    tailored: dict[str, TailoredFile] = field(default_factory=dict)
    held: int = 0
    ai_on: bool = False  # scoring is configured: a job without a score says the analysis is down


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
        job_page_spacing: tuple[float, float] = (5.0, 10.0),
        clock: Callable[[], datetime] = _utc_now,
        assessor: Assessor | None = None,
        tailor: Tailor | None = None,
        heartbeat: Callable[[], None] = lambda: None,
    ) -> None:
        self._source = source
        self._heartbeat = heartbeat
        self._assessor = assessor
        self._tailor = tailor
        self._dispatcher = dispatcher
        self._store = store
        self._searches = list(searches)
        self._location = location
        self._interval = interval_seconds
        self._search_spacing = search_spacing
        self._job_page_spacing = job_page_spacing
        self._clock = clock
        # Jobs already held once because LinkedIn blocked their page: the next time they go out without a score.
        self._held: set[str] = set()
        self._failing_since: datetime | None = None
        self._down_alert_sent = False
        self._next_digest: datetime | None = None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for step in (self.digest_if_due, self.cycle):
                self._heartbeat()  # alive: the longest silence is one step or one sleep, never both
                try:
                    await step()
                except Exception:
                    # A bug in one step must not end the worker: the next loop starts clean.
                    logger.exception("%s failed", step.__name__)
            self._heartbeat()
            delay = self._interval * random.uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)  # noqa: S311
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def cycle(self) -> None:
        now = self._clock()
        new: dict[str, Job] = {}
        state_changed = False
        answered = await self._read_searches(CYCLE_WINDOW_SECONDS)
        failed = len(self._searches) - len(answered)

        for keywords, jobs in answered:
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
        # A quiet cycle logs at DEBUG; the digest line every 4 hours shows the worker is alive.
        logger.log(
            logging.INFO if new or failed else logging.DEBUG,
            "Cycle done: %d of %d search(es) answered, %d new job(s)",
            len(self._searches) - failed,
            len(self._searches),
            len(new),
        )

    async def digest_if_due(self) -> None:
        """Every DIGEST_EVERY: up to MAX_PER_MESSAGE jobs from the last 3 days not sent yet, in one message."""
        now = self._clock()
        if self._next_digest is not None and now < self._next_digest:
            return
        answered = await self._read_searches(DIGEST_WINDOW_SECONDS)
        if not answered:
            return  # source down: the next loop tries again
        pending: dict[str, Job] = {}
        for _, jobs in answered:
            for job in jobs:
                if not self._store.is_seen(job.key):
                    pending.setdefault(job.key, job)
        batch = await self._assess(list(pending.values())[:MAX_PER_MESSAGE], now)
        picked, held = batch.jobs, batch.held
        if picked:
            message = job_message(picked, batch.scores, batch.tailored, ai_on=batch.ai_on)
            channel = await self._dispatcher.deliver(message)
            if channel is None:
                return  # left unmarked and still due: the next loop tries again
            self._store.mark(message.keys, now)
            await self._store.save()
        if held:
            logger.info("Digest sent %d job(s); %d held until the job pages answer", len(picked), held)
            return  # still due: the next loop scores the held jobs
        self._next_digest = now + DIGEST_EVERY
        logger.info("Digest done: %d of %d pending job(s) sent", len(picked), len(pending))

    async def _read_searches(self, window_seconds: int) -> list[tuple[str, list[Job]]]:
        """Each search that answered, with its jobs. A failed search is logged and left out.

        A block stops the read: the other searches would only fail too, one warning each.
        """
        answered: list[tuple[str, list[Job]]] = []
        for index, keywords in enumerate(self._searches):
            if index:
                await asyncio.sleep(random.uniform(*self._search_spacing))  # noqa: S311
            try:
                jobs = await self._source.fetch_recent(keywords, self._location, window_seconds=window_seconds)
            except SourceUnavailableError as e:
                logger.warning("Search '%s' failed: %s", keywords, e)
                if e.blocked:
                    break
                continue
            terms = excluded_terms(keywords)
            kept = [job for job in jobs if not is_excluded(job.title, terms)]
            if len(kept) < len(jobs):
                logger.info("Search '%s': %d job(s) dropped by a NOT term", keywords, len(jobs) - len(kept))
            answered.append((keywords, kept))
        return answered

    async def _assess(self, jobs: list[Job], now: datetime) -> _Batch:
        """Scores each job, best first; a score from TAILOR_MIN_SCORE up also gets the tailored resume.

        On-site and closed jobs are dropped and marked as seen: they never go out. When LinkedIn blocks the job
        pages, the unscored jobs are held (not sent, not marked) once, so the next run can score them; held a
        second time, they go out without a score. Any other failure (page broken, LLM down) sends them unscored.
        """
        if self._assessor is None:
            return _Batch(jobs)
        scores: dict[str, Assessment] = {}
        tailored: dict[str, TailoredFile] = {}
        held: set[str] = set()
        closed: set[str] = set()
        blocked = False
        fetched = 0
        for job in jobs:
            assessment = self._assessor.cached(job)
            details = None
            if assessment is None and not blocked:
                if fetched:
                    await asyncio.sleep(random.uniform(*self._job_page_spacing))  # noqa: S311
                fetched += 1
                try:
                    details = await self._source.fetch_details(job)
                except SourceUnavailableError as e:
                    logger.warning("Job page of %s unavailable: %s", job.key, e)
                    blocked = e.blocked  # a block stops the job pages for the rest of this batch
                else:
                    if details.closed:
                        closed.add(job.key)  # no score needed: it is dropped, so no quota spent
                        continue
                    assessment = await self._assessor.assess(job, details)
            if assessment is not None:
                scores[job.key] = assessment
                resume = await self._tailored(job, assessment, details)
                if resume is not None:
                    tailored[job.key] = resume
            elif blocked and job.key not in self._held:
                held.add(job.key)
        onsite = {key for key, assessment in scores.items() if assessment.workplace == "onsite"}
        dropped = onsite | closed
        if dropped:
            self._store.mark(dropped, now)
            await self._store.save()
            logger.info("Dropped %d on-site and %d closed job(s)", len(onsite), len(closed))
        if held:
            logger.warning("Held %d job(s) until LinkedIn lets the job pages through", len(held))
        kept = [job for job in jobs if job.key not in dropped | held]
        kept.sort(key=lambda job: scores[job.key].score if job.key in scores else -1, reverse=True)
        if len(self._held) > _HELD_LIMIT:
            self._held.clear()
        self._held = (self._held | held) - {job.key for job in kept}
        return _Batch(kept, scores, tailored, len(held), ai_on=True)

    async def _tailored(self, job: Job, assessment: Assessment, details: JobDetails | None) -> TailoredFile | None:
        if self._tailor is None or assessment.score < TAILOR_MIN_SCORE or assessment.workplace == "onsite":
            return None
        # Without the page (the score came from the cache) only an already built file can be used.
        return self._tailor.cached(job) if details is None else await self._tailor.tailor(job, details)

    async def _notify(self, jobs: list[Job], now: datetime) -> None:
        batch = await self._assess(jobs, now)
        for group in batches(batch.jobs):
            message = job_message(group, batch.scores, batch.tailored, ai_on=batch.ai_on)
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
