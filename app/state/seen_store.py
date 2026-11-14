import asyncio
import logging
import os
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# The 6-hour list reads jobs of any date, and a LinkedIn posting can stay open for months: a key kept for
# less would bring an old job back, scored and sent again. A key is ~25 bytes: a year is well under 1 MB.
MAX_AGE = timedelta(days=365)


class _State(BaseModel):
    version: int = 1
    searches: list[str]
    # Job key -> day it was first seen. Nothing about the job itself is kept (LGPD: data minimization).
    seen: dict[str, str]


class SeenStore:
    """Jobs already notified, plus the searches that already had their baseline.

    Memory only by default. With a path, the state is loaded at boot and rewritten after each change
    through a temp file and a rename, so a crash mid-write never leaves half a file behind.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._seen: dict[str, str] = {}
        self._searches: set[str] = set()

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            state = _State.model_validate_json(self._path.read_bytes())
        except (OSError, ValidationError) as e:
            logger.error("Unreadable state file (%s): every search starts a new baseline", type(e).__name__)
            return
        self._seen = state.seen
        self._searches = set(state.searches)
        logger.info("Loaded %d seen jobs and %d searches from the state file", len(self._seen), len(self._searches))

    def has_baseline(self, search: str) -> bool:
        return search in self._searches

    def add_baseline(self, search: str) -> None:
        self._searches.add(search)

    def is_seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, keys: Iterable[str], now: datetime) -> None:
        today = now.date().isoformat()
        for key in keys:
            self._seen.setdefault(key, today)

    def prune(self, now: datetime) -> bool:
        """Drops keys older than MAX_AGE. True when something went."""
        cutoff = (now - MAX_AGE).date()
        expired = [key for key, day in self._seen.items() if _day(day) < cutoff]
        for key in expired:
            del self._seen[key]
        return bool(expired)

    async def save(self) -> None:
        if self._path is None:
            return
        payload = _State(searches=sorted(self._searches), seen=self._seen).model_dump_json()
        try:
            await asyncio.to_thread(_write_atomic, self._path, payload)
        except OSError as e:
            # Memory still holds the state: the worker keeps going and a restart is what would repeat alerts.
            logger.error("Could not write the state file: %s", e)


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return date.min


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
