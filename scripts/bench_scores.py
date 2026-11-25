# ruff: noqa: T201  (a CLI: printing is its output)
"""Bench: each job's score against the base resume next to its score against the tailored file, on real jobs,
with what is still missing after tailoring and the gaps most jobs ask for.

Reads .env like the worker (LLM key, searches, location) and uses its first search on LinkedIn and Gupy.
Spends quota: 1 LLM call per job, 3 per job scored MIN_SCORE or more. Nothing is sent and nothing is marked.
Run from the repo root: venv/Scripts/python.exe -m scripts.bench_scores [jobs per source, default 5]
"""

import asyncio
import random
import sys
from collections import Counter

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
        missing: Counter[str] = Counter()  # gaps left after tailoring: what the resume itself lacks
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
                after = tailored.assessment if tailored else None
                shown = "-" if after is None else f"{after.score} ({after.score - base.score:+d})"
                print(f"{source.name:8} base {base.score:3} | ajustado {shown:10} | {job.title}")
                if after is not None:
                    deltas.append(after.score - base.score)
                    missing.update(gap.casefold() for gap in after.gaps)
                    print(f"{'':8} {after.reasons}")
                    if after.gaps:
                        print(f"{'':8} ainda falta: {', '.join(after.gaps)}")
        if deltas:
            print(f"\n{len(deltas)} vaga(s) {MIN_SCORE}+: diferença média {sum(deltas) / len(deltas):+.1f}")
            print(f"menor {min(deltas):+d}, maior {max(deltas):+d}")
        if missing:
            print("\nO que mais falta no currículo, mesmo ajustado (vagas que pedem):")
            for gap, count in missing.most_common(10):
                print(f"{count:3}  {gap}")


if __name__ == "__main__":
    asyncio.run(bench(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
