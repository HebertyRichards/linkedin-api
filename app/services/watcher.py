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
from app.services.tailor import MIN_SCORE, Tailor, TailoredFile
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
# The digest reads jobs of any date: it lists what fits the resume, however old the posting. Every 6 hours,
# never at boot: each digest scores up to 10 jobs not seen yet, and the LLM free quota has to last the day.
DIGEST_EVERY = timedelta(hours=6)
# A digest scores the unseen jobs 10 at a time and goes on until one scores MIN_SCORE or more (the ones below
# are marked as seen, so each round moves forward). At most this many rounds: each one spends up to 10 LLM
# scores and 10 job pages on LinkedIn.
MAX_DIGEST_ROUNDS = 5
# A job page failing this many times without a block (broken page) is given up: marked as seen, never sent.
_MAX_PAGE_FAILURES = 3
# Bound for the failure counts; past it they are forgotten (worst case a broken page gets 3 more tries).
_FAILURES_LIMIT = 1000


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
    pending: int = 0  # not scored yet (job page or LLM down): neither sent nor marked, so a later run scores them


@dataclass(frozen=True)
class DigestResult:
    sent: int  # jobs scored MIN_SCORE or more, delivered
    checked: int  # jobs not seen yet that this digest looked at (up to MAX_DIGEST_ROUNDS x MAX_PER_MESSAGE)
    pending: int  # of those, the ones left without a score for now


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Watcher:
    """One cycle: read every search, score the jobs not seen yet, deliver the ones scored MIN_SCORE or more,
    remember what was handled. A job below it is marked as seen and never goes out."""

    def __init__(
        self,
        *,
        source: JobSource,
        dispatcher: Dispatcher,
        store: SeenStore,
        searches: Sequence[str],
        location: str,
        interval_seconds: int,
        assessor: Assessor,
        search_spacing: tuple[float, float] = (2.0, 5.0),
        job_page_spacing: tuple[float, float] = (5.0, 10.0),
        clock: Callable[[], datetime] = _utc_now,
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
        self._page_failures: dict[str, int] = {}
        self._failing_since: datetime | None = None
        self._down_alert_sent = False
        self._lock = asyncio.Lock()
        self.last_cycle_at: datetime | None = None
        # Not at boot: a deploy starts the cycle only, and the first digest comes DIGEST_EVERY later.
        self._next_digest = clock() + DIGEST_EVERY

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for step in (self.digest_if_due, self.cycle):
                self._heartbeat()  # alive: the longest silence is one step or one sleep, never both
                try:
                    async with self._lock:
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
        self.last_cycle_at = now
        # A quiet cycle logs at DEBUG; the digest line every 6 hours shows the worker is alive.
        logger.log(
            logging.INFO if new or failed else logging.DEBUG,
            "Cycle done: %d of %d search(es) answered, %d new job(s)",
            len(self._searches) - failed,
            len(self._searches),
            len(new),
        )

    @property
    def next_digest_at(self) -> datetime:
        return self._next_digest

    async def digest_if_due(self) -> None:
        """Every DIGEST_EVERY: the digest. When it fails it stays due, and the next loop tries again."""
        now = self._clock()
        if now < self._next_digest:
            return
        if await self._digest(now) is not None:
            self._next_digest = now + DIGEST_EVERY

    async def digest_now(self) -> DigestResult | None:
        """The digest on request (/resumo on Telegram). The 6-hour clock does not move: the next automatic
        digest keeps its time. None: the source or every channel is down."""
        async with self._lock:
            return await self._digest(self._clock())

    async def _digest(self, now: datetime) -> DigestResult | None:
        """Scores the jobs of any date not seen yet, MAX_PER_MESSAGE at a time, until a round has one scored
        MIN_SCORE or more, and sends that round's in one message. A round with a job left unscored (job page or
        LLM down) ends the digest: the next rounds would fail too. Nothing from MIN_SCORE up after
        MAX_DIGEST_ROUNDS: no message. None: the source or every channel is down."""
        answered = await self._read_searches(None)
        if not answered:
            return None
        unseen: dict[str, Job] = {}
        for _, jobs in answered:
            for job in jobs:
                if not self._store.is_seen(job.key):
                    unseen.setdefault(job.key, job)
        queue = list(unseen.values())[: MAX_DIGEST_ROUNDS * MAX_PER_MESSAGE]
        batch = _Batch([])
        checked = pending = 0
        for start in range(0, len(queue), MAX_PER_MESSAGE):
            self._heartbeat()  # a round can take long (LLM per-minute limit): the watchdog must not see a stall
            group = queue[start : start + MAX_PER_MESSAGE]
            batch = await self._assess(group, now)
            checked += len(group)
            pending += batch.pending
            if batch.jobs or batch.pending:
                break
        if batch.jobs:
            message = job_message(batch.jobs, batch.scores, batch.tailored)
            channel = await self._dispatcher.deliver(message)
            if channel is None:
                return None  # left unmarked: the jobs go out in a later run
            self._store.mark(message.keys, now)
            await self._store.save()
        logger.info("Digest done: %d job(s) sent, %d scored of %d not seen yet", len(batch.jobs), checked, len(unseen))
        return DigestResult(len(batch.jobs), checked, pending)

    async def _read_searches(self, window_seconds: int | None) -> list[tuple[str, list[Job]]]:
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
        """Scores each job and keeps the ones scored MIN_SCORE or more, best first, each with its tailored resume.

        Low-scored, on-site and closed jobs are dropped and marked as seen: they never go out. A job without a
        score (job page or LLM down) is pending: neither sent nor marked, so the next run scores it. Only a job
        page that keeps failing without a block gives up after _MAX_PAGE_FAILURES, so it cannot fill every digest.
        """
        scores: dict[str, Assessment] = {}
        tailored: dict[str, TailoredFile] = {}
        closed: set[str] = set()
        pending = 0
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
                    if not blocked and self._page_failed(job):
                        closed.add(job.key)
                        continue
                else:
                    if details.closed:
                        closed.add(job.key)  # no score needed: it is dropped, so no quota spent
                        continue
                    assessment = await self._assessor.assess(job, details)
            if assessment is None:
                pending += 1
                continue
            scores[job.key] = assessment
            if assessment.score >= MIN_SCORE and assessment.workplace != "onsite":
                resume = await self._tailored(job, details)
                if resume is not None:
                    tailored[job.key] = resume
        onsite = {key for key, assessment in scores.items() if assessment.workplace == "onsite"}
        low = {key for key, assessment in scores.items() if assessment.score < MIN_SCORE} - onsite
        dropped = low | onsite | closed
        if dropped:
            self._store.mark(dropped, now)
            await self._store.save()
            logger.info(
                "Dropped %d job(s) scored below %d, %d on-site and %d closed",
                len(low),
                MIN_SCORE,
                len(onsite),
                len(closed),
            )
        if pending:
            logger.warning("%d job(s) not scored yet: they wait for the next run", pending)
        kept = [job for job in jobs if job.key in scores and job.key not in dropped]
        kept.sort(key=lambda job: scores[job.key].score, reverse=True)
        return _Batch(kept, scores, tailored, pending)

    def _page_failed(self, job: Job) -> bool:
        """Counts a job page failure that was not a block. True when the job is given up."""
        if len(self._page_failures) > _FAILURES_LIMIT:
            self._page_failures.clear()
        failures = self._page_failures.get(job.key, 0) + 1
        self._page_failures[job.key] = failures
        if failures < _MAX_PAGE_FAILURES:
            return False
        logger.warning("Job page of %s failed %d times: given up", job.key, failures)
        del self._page_failures[job.key]
        return True

    async def _tailored(self, job: Job, details: JobDetails | None) -> TailoredFile | None:
        if self._tailor is None:
            return None
        # Without the page (the score came from the cache) only an already built file can be used.
        return self._tailor.cached(job) if details is None else await self._tailor.tailor(job, details)

    async def _notify(self, jobs: list[Job], now: datetime) -> None:
        batch = await self._assess(jobs, now)
        for group in batches(batch.jobs):
            message = job_message(group, batch.scores, batch.tailored)
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
