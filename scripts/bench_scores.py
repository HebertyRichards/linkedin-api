# ruff: noqa: T201  (a CLI: printing is its output)
"""Bench: each job's score against the base resume next to its score against the tailored file, on real jobs.

Reads .env like the worker (LLM key, searches, location) and uses its first search on LinkedIn and Gupy.
Spends quota: 1 LLM call per job, 3 per job scored MIN_SCORE or more. Nothing is sent and nothing is marked.
Run from the repo root: venv/Scripts/python.exe -m scripts.bench_scores [jobs per source, default 5]
"""

import asyncio
import random
import sys

import httpx

from app.config.settings import get_settings
from app.errors import SourceUnavailableError
from app.main import _ai_services, _profile
from app.services.tailor import MIN_SCORE
from app.sources.base import JobSource
from app.sources.gupy_portal import GupyPortalSource
from app.sources.linkedin_guest import LinkedInGuestSource


async def bench(limit: int) -> None:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        assessor, tailor = _ai_services(client, settings, _profile(settings))
        sources: list[JobSource] = [LinkedInGuestSource(client), GupyPortalSource(client)]
        deltas: list[int] = []
        for source in sources:
            try:
                jobs = await source.fetch_recent(settings.searches[0], settings.job_location, window_seconds=None)
            except SourceUnavailableError as e:
                print(f"{source.name}: busca indisponível ({e})")
                continue
            for index, job in enumerate(jobs[:limit]):
                if index and source.pages_need_requests:
                    await asyncio.sleep(random.uniform(5.0, 10.0))  # noqa: S311  same spacing as the worker
                try:
                    details = await source.fetch_details(job)
                except SourceUnavailableError as e:
                    print(f"{source.name} {job.title}: página indisponível ({e})")
                    continue
                if details.closed:
                    continue
                base = await assessor.assess(job, details)
                if base is None:
                    print(f"{source.name} {job.title}: sem nota (IA fora)")
                    continue
                tailored = await tailor.tailor(job, details) if base.score >= MIN_SCORE else None
                after = tailored.score if tailored else None
                if after is not None:
                    deltas.append(after - base.score)
                shown = "-" if after is None else f"{after} ({after - base.score:+d})"
                print(f"{source.name:8} base {base.score:3} | ajustado {shown:10} | {job.title}")
        if deltas:
            print(f"\n{len(deltas)} vaga(s) {MIN_SCORE}+: diferença média {sum(deltas) / len(deltas):+.1f}")
            print(f"menor {min(deltas):+d}, maior {max(deltas):+d}")


if __name__ == "__main__":
    asyncio.run(bench(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
