import json
from datetime import UTC, datetime

import httpx
import pytest

from app.errors import SourceUnavailableError
from app.schemas.job import Job
from app.sources.himalayas import HimalayasSource

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
CLASH_URL = "https://himalayas.app/companies/clash/jobs/desenvolvedor-a-fullstack"


def item(company: str, slug: str, **fields: object) -> dict[str, object]:
    """One job as the search JSON has it (fields seen on 2026-11-18); `fields` override the defaults."""
    return {
        "title": "Desenvolvedor Python",
        "excerpt": "Vaga para Python.",
        "companyName": "Acme",
        "companySlug": company,
        "employmentType": "Full Time",
        "seniority": ["Mid-level"],
        "locationRestrictions": ["Brazil"],
        "description": "<p>Vaga para <strong>Python</strong> &amp; FastAPI.</p>",
        "pubDate": NOW_TS - 1800,
        "expiryDate": NOW_TS + 30 * 86400,
        "applicationLink": f"https://himalayas.app/companies/{company}/jobs/{slug}/apply",
        "guid": f"https://himalayas.app/companies/{company}/jobs/{slug}",
        **fields,
    }


def answer(*items: object) -> str:
    """A search answer with the envelope the API returns."""
    data = {"comments": "", "updatedAt": NOW_TS, "offset": 0, "limit": 20, "totalCount": len(items)}
    return json.dumps({**data, "jobs": list(items)})


def source(client: httpx.AsyncClient) -> HimalayasSource:
    return HimalayasSource(client, term_delay=0, clock=lambda: NOW)


def serving(body: str, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body)))


async def test_one_request_per_term_for_brazil_newest_first_without_cookies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await source(client).fetch_recent('("full stack" OR next.js) NOT senior', "Brazil", window_seconds=3600)

    assert [r.url.params["q"] for r in requests] == ["full stack", "next.js"]
    for request in requests:
        assert (request.url.scheme, request.url.host, request.url.path) == (
            "https",
            "himalayas.app",
            "/jobs/api/search",
        )
        assert (request.url.params["country"], request.url.params["sort"]) == ("BR", "recent")
        assert request.headers["user-agent"] == "linkedin-api-job-alert/0.1"
        assert request.headers["accept"] == "application/json"
        assert "cookie" not in request.headers


async def test_job_is_parsed_and_its_link_rebuilt_from_the_guid() -> None:
    body = answer(
        item("clash", "desenvolvedor-a-fullstack", title="Desenvolvedor(a) Fullstack", companyName="Clash"),
        item("acme", "dev-2", locationRestrictions=["Brazil", "Argentina"]),
        item("acme", "dev-3", locationRestrictions=[]),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    clash = jobs[0]
    assert (clash.id, clash.title, clash.company, clash.source) == (
        "clash/desenvolvedor-a-fullstack",
        "Desenvolvedor(a) Fullstack",
        "Clash",
        "himalayas",
    )
    assert clash.url == CLASH_URL
    assert [j.location for j in jobs] == ["Remoto (Brazil)", "Remoto (Brazil, Argentina)", "Remoto"]


async def test_invalid_jobs_are_skipped_and_valid_ones_remain() -> None:
    body = answer(
        item("acme", "dev-1", guid="https://evil.com/companies/acme/jobs/dev-1"),
        item("acme", "dev-2", guid="https://himalayas.app/companies/acme/jobs/dev-2/apply"),
        item("acme", "dev-3", guid="https://himalayas.app/companies/Acme/jobs/dev-3"),
        item("acme", "dev-4", guid="http://himalayas.app/companies/acme/jobs/dev-4"),
        item("acme", "dev-5", guid="https://himalayas.app/companies/acme/jobs/dev-5?x=1"),
        item("acme", "dev-6", pubDate=None),
        item("acme", "dev-7", title=None),
        "not a job",
        item("acme", "dev-8"),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["acme/dev-8"]


async def test_window_keeps_only_jobs_published_inside_it() -> None:
    body = answer(item("acme", "new", pubDate=NOW_TS - 1800), item("acme", "old", pubDate=NOW_TS - 5400))
    async with serving(body) as client:
        recent = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)
        any_date = await source(client).fetch_recent("python", "Brazil", window_seconds=None)

    assert [j.id for j in recent] == ["acme/new"]
    assert [j.id for j in any_date] == ["acme/new", "acme/old"]


async def test_a_job_found_by_two_terms_comes_once() -> None:
    async with serving(answer(item("acme", "dev-1"), item("acme", "dev-2"))) as client:
        jobs = await source(client).fetch_recent("python OR django", "Brazil", window_seconds=3600)

    assert [j.key for j in jobs] == ["himalayas:acme/dev-1", "himalayas:acme/dev-2"]


async def test_answer_where_no_job_is_recognized_is_a_failure() -> None:
    # A renamed field makes every item invalid: that is an API change, not "no new jobs".
    async with serving(answer(item("acme", "dev-1", guid=None), item("acme", "dev-2", guid=None))) as client:
        with pytest.raises(SourceUnavailableError, match="no job recognized"):
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)


async def test_answer_where_no_job_has_a_description_is_a_failure() -> None:
    # The description renamed or gone: every job would be scored on its title alone, so it is an API change.
    body = answer(item("acme", "dev-1", description=None), item("acme", "dev-2", description="<p> </p>"))
    async with serving(body) as client:
        with pytest.raises(SourceUnavailableError, match="no job description"):
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)


@pytest.mark.parametrize("status", [403, 429])
async def test_block_answer_opens_the_circuit(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        himalayas = source(client)
        with pytest.raises(SourceUnavailableError) as blocked:
            await himalayas.fetch_recent("python OR django", "Brazil", window_seconds=3600)
        assert blocked.value.blocked
        assert blocked.value.retry_after == 120
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await himalayas.fetch_recent("python", "Brazil", window_seconds=3600)

    assert len(requests) == 1


async def test_other_status_is_a_failure_not_a_block() -> None:
    async with serving("", status=500) as client:
        himalayas = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="status 500") as failed:
                await himalayas.fetch_recent("python", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await himalayas.fetch_recent("python", "Brazil", window_seconds=3600)


async def test_network_error_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceUnavailableError, match="network error: ConnectTimeout") as failed:
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert not failed.value.blocked


@pytest.mark.parametrize(
    "body",
    ["<html><body><h1>Checking your browser</h1></body></html>", '{"comments": "", "offset": 0}'],
    ids=["not-json", "no-job-list"],
)
async def test_answer_without_the_job_list_is_a_failure(body: str) -> None:
    async with serving(body) as client:
        himalayas = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="layout changed") as failed:
                await himalayas.fetch_recent("python", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await himalayas.fetch_recent("python", "Brazil", window_seconds=3600)


async def test_empty_search_is_zero_jobs_not_a_failure() -> None:
    async with serving(answer()) as client:
        assert await source(client).fetch_recent("python", "Brazil", window_seconds=3600) == []


async def test_details_come_from_the_search_without_a_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=answer(
                item("acme", "open"),
                item("acme", "expired", seniority=["Mid-level", "Senior"], expiryDate=NOW_TS - 60),
                item("acme", "no-text", description=""),
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        himalayas = source(client)
        open_job, expired, no_text = await himalayas.fetch_recent("python", "Brazil", window_seconds=3600)
        details = await himalayas.fetch_details(open_job)
        closed = await himalayas.fetch_details(expired)
        title_only = await himalayas.fetch_details(no_text)
        with pytest.raises(SourceUnavailableError) as missing:
            await himalayas.fetch_details(Job(id="acme/not-searched", title="Not searched", source="himalayas"))

    assert (details.description, details.seniority, details.employment_type, details.closed) == (
        "Vaga para Python & FastAPI.",
        "Mid-level",
        "Full Time",
        False,
    )
    assert (closed.seniority, closed.closed) == ("Mid-level, Senior", True)
    # Scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
    assert title_only.description == "Desenvolvedor Python"
    assert not missing.value.blocked
    assert len(requests) == 1
