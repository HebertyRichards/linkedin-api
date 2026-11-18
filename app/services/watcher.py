import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.errors import SourceUnavailableError
from app.schemas.job import Assessment, Job, JobDetails, Source
from app.services.assessor import Assessor
from app.services.dispatcher import Dispatcher
from app.services.messages import MAX_PER_MESSAGE, batches, job_message, source_down_message
from app.services.tailor import MIN_SCORE, Tailor, TailoredFile
from app.sources.base import JobSource
from app.state.seen_store import SeenStore
from app.utils.search_query import excluded_terms, is_excluded

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
# /vagas on Telegram: this many jobs of any date, each with its score, whatever the score.
LIST_SIZE = 20
# A job page failing this many times without a block (broken page) is given up: marked as seen, never sent.
_MAX_PAGE_FAILURES = 3
# Bound for the failure counts; past it they are forgotten (worst case a broken page gets 3 more tries).
_FAILURES_LIMIT = 1000


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
    complete: bool = True  # every source answered; False keeps the automatic digest due


@dataclass
class _Pages:
    """The job pages of one run, per source: spaced from the same source's previous one, and none after its
    block (a LinkedIn block does not hold the other sources' pages)."""

    fetched: set[Source] = field(default_factory=set)
    blocked: set[Source] = field(default_factory=set)


@dataclass(frozen=True)
class ListResult:
    shown: int  # jobs listed in the message
    scored: int  # of those, the ones with a score (the others: job page or LLM down)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Watcher:
    """One cycle: read every search, score the jobs not seen yet, deliver the ones scored MIN_SCORE or more,
    remember what was handled. A job below it is marked as seen and never goes out."""

    def __init__(
        self,
        *,
        sources: Sequence[JobSource],
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
        self._sources = {source.name: source for source in sources}
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
        self._last_read: dict[Source, datetime] = {}  # when the cycle last read each source
        self._failing_since: dict[Source, datetime] = {}
        self._down_alert_sent: set[Source] = set()
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
        read = answered_count = 0
        for source in self._sources.values():
            if not self._due(source, now):
                continue
            self._last_read[source.name] = now
            answered = await self._read_searches(source, CYCLE_WINDOW_SECONDS)
            read += len(self._searches)
            answered_count += len(answered)
            await self._track_source_health(source.name, now, all_failed=not answered)
            for keywords, jobs in answered:
                # The old LinkedIn ids ("keywords|location") are left behind: LinkedIn takes a new baseline on its
                # first cycle after the switch. Wanted: its search changed (no Easy Apply filter), and that first
                # cycle would otherwise score a flood of jobs.
                search_id = f"{source.name}|{keywords}|{self._location}".lower()
                if not self._store.has_baseline(search_id):
                    # First successful read of this search: what is already posted is not news.
                    self._store.mark((job.key for job in jobs), now)
                    self._store.add_baseline(search_id)
                    state_changed = True
                    logger.info(
                        "Baseline for %s '%s': %d current jobs marked as seen", source.name, keywords, len(jobs)
                    )
                    continue
                for job in jobs:
                    if not self._store.is_seen(job.key):
                        new.setdefault(job.key, job)

        if new:
            await self._notify(list(new.values()), now)
        if self._store.prune(now) or state_changed:
            await self._store.save()
        self.last_cycle_at = now
        # A quiet cycle logs at DEBUG; the digest line every 6 hours shows the worker is alive.
        logger.log(
            logging.INFO if new or answered_count < read else logging.DEBUG,
            "Cycle done: %d of %d search(es) answered, %d new job(s)",
            answered_count,
            read,
            len(new),
        )

    def _due(self, source: JobSource, now: datetime) -> bool:
        """Whether the cycle reads `source` now: its min_interval passed since the cycle last read it."""
        last = self._last_read.get(source.name)
        if source.min_interval is None or last is None:
            return True
        # The loop sleep moves ±20%: a source due every 30 min with a 15 min loop is not pushed to the third loop.
        return now - last >= source.min_interval * (1 - INTERVAL_JITTER)

    @property
    def next_digest_at(self) -> datetime:
        return self._next_digest

    async def digest_if_due(self) -> None:
        """Every DIGEST_EVERY: the digest. When it fails, or a source did not answer, it stays due and the next
        loop tries again: a source back from an outage gets its catch-up then, not 6 hours later."""
        now = self._clock()
        if now < self._next_digest:
            return
        result = await self._digest(now)
        if result is not None and result.complete:
            self._next_digest = now + DIGEST_EVERY

    async def digest_now(self) -> DigestResult | None:
        """The digest on request (/resumo on Telegram). The 6-hour clock does not move: the next automatic
        digest keeps its time. None: every source or every channel is down."""
        async with self._lock:
            return await self._digest(self._clock())

    async def list_now(self) -> ListResult | None:
        """/vagas on Telegram: the first LIST_SIZE jobs of any date, seen or not, each with its score, best first.
        Only a view: no resume is tailored and only a closed job (left out of the list) is marked as seen, so the
        cycle and the digest go on as before: a job from MIN_SCORE up still comes in the digest, with its resume.
        None: every source or every channel is down."""
        async with self._lock:
            answered, _ = await self._read_every_source(None)
            if not answered:
                return None
            pool: dict[str, Job] = {}
            for jobs in answered:
                for job in jobs:
                    pool.setdefault(job.key, job)
            listed: list[Job] = []
            scores: dict[str, Assessment] = {}
            closed: list[str] = []
            pages = _Pages()
            for job in pool.values():
                if len(listed) == LIST_SIZE:
                    break
                self._heartbeat()  # 20 job pages and scores can take long: the watchdog must not see a stall
                assessment = self._assessor.cached(job)
                if assessment is None:
                    details = await self._page(job, pages)  # None: listed without a score
                    if details is not None and details.closed:
                        closed.append(job.key)  # marked, so the next /vagas does not open its page again
                        continue
                    if details is not None:
                        assessment = await self._assessor.assess(job, details)
                listed.append(job)
                if assessment is not None:
                    scores[job.key] = assessment
            if closed:
                self._store.mark(closed, self._clock())
                await self._store.save()
            if listed:
                listed.sort(key=lambda job: scores[job.key].score if job.key in scores else -1, reverse=True)
                message = job_message(listed, scores, heading=f"{len(listed)} vagas, todas as notas")
                if await self._dispatcher.deliver(message, copy=False) is None:
                    return None
            logger.info("List done: %d job(s) sent, %d scored", len(listed), len(scores))
            return ListResult(len(listed), len(scores))

    async def _digest(self, now: datetime) -> DigestResult | None:
        """Scores the jobs of any date not seen yet, MAX_PER_MESSAGE at a time, until a round has one scored
        MIN_SCORE or more, and sends that round's in one message. A round with a job left unscored (job page or
        LLM down) ends the digest: the next rounds would fail too. Nothing from MIN_SCORE up after
        MAX_DIGEST_ROUNDS: no message. None: every source or every channel is down."""
        answered, complete = await self._read_every_source(None)
        if not answered:
            return None
        unseen: dict[str, Job] = {}
        for jobs in answered:
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
        return DigestResult(len(batch.jobs), checked, pending, complete)

    async def _read_every_source(self, window_seconds: int | None) -> tuple[list[list[Job]], bool]:
        """The jobs of each search that answered, from every source whatever its min_interval (digest, /vagas),
        and whether every source answered at least one search."""
        answered: list[list[Job]] = []
        complete = True
        for source in self._sources.values():
            searches = await self._read_searches(source, window_seconds)
            complete = complete and bool(searches)
            answered += [jobs for _, jobs in searches]
        return answered, complete

    async def _read_searches(self, source: JobSource, window_seconds: int | None) -> list[tuple[str, list[Job]]]:
        """Each search that `source` answered, with its jobs. A failed search is logged and left out.

        A block stops this source's read: its other searches would only fail too, one warning each.
        """
        answered: list[tuple[str, list[Job]]] = []
        for index, keywords in enumerate(self._searches):
            if index:
                await asyncio.sleep(random.uniform(*self._search_spacing))  # noqa: S311
            try:
                jobs = await source.fetch_recent(keywords, self._location, window_seconds=window_seconds)
            except SourceUnavailableError as e:
                logger.warning("%s search '%s' failed: %s", source.name, keywords, e)
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
        pages = _Pages()
        for job in jobs:
            assessment = self._assessor.cached(job)
            details = None
            if assessment is None and job.source not in pages.blocked:
                details = await self._page(job, pages)
                if details is None:
                    if job.source not in pages.blocked and self._page_failed(job):
                        closed.add(job.key)
                        continue
                elif details.closed:
                    closed.add(job.key)  # no score needed: it is dropped, so no quota spent
                    continue
                else:
                    assessment = await self._assessor.assess(job, details)
            if assessment is None:
                pending += 1
                continue
            scores[job.key] = assessment
            if assessment.score >= MIN_SCORE and assessment.workplace != "onsite":
                resume = await self._tailored(job, details, pages)
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

    async def _page(self, job: Job, pages: _Pages) -> JobDetails | None:
        """The job page from the job's own source, spaced from that source's previous one in the run. None:
        unavailable, or a block of that source earlier in the run (it stops that source's pages for the run)."""
        if job.source in pages.blocked:
            return None
        if job.source in pages.fetched:
            await asyncio.sleep(random.uniform(*self._job_page_spacing))  # noqa: S311
        pages.fetched.add(job.source)
        try:
            return await self._sources[job.source].fetch_details(job)
        except SourceUnavailableError as e:
            logger.warning("Job page of %s unavailable: %s", job.key, e)
            if e.blocked:
                pages.blocked.add(job.source)
            return None

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

    async def _tailored(self, job: Job, details: JobDetails | None, pages: _Pages) -> TailoredFile | None:
        if self._tailor is None:
            return None
        if details is None:  # the score came from the cache: the file built then, or the page to build one now
            cached = self._tailor.cached(job)
            if cached is not None:
                return cached
            details = await self._page(job, pages)  # scored without a file (/vagas, or the LLM was down)
            if details is None or details.closed:
                return None
        return await self._tailor.tailor(job, details)

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

    async def _track_source_health(self, name: Source, now: datetime, *, all_failed: bool) -> None:
        """For a source the cycle read: an alert once per outage, after SOURCE_DOWN_ALERT_AFTER of failures."""
        if not all_failed:
            if self._failing_since.pop(name, None) is not None:
                logger.info("Job source %s recovered", name)
            self._down_alert_sent.discard(name)
            return
        since = self._failing_since.setdefault(name, now)
        if name not in self._down_alert_sent and now - since >= SOURCE_DOWN_ALERT_AFTER:
            if await self._dispatcher.deliver(source_down_message(since, name)) is not None:
                self._down_alert_sent.add(name)
