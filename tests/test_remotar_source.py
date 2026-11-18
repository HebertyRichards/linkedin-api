import json
from datetime import UTC, datetime

import httpx
import pytest

from app.errors import SourceUnavailableError
from app.schemas.job import Job
from app.sources.remotar import RemotarSource

NOW = datetime(2026, 11, 17, 19, 0, tzinfo=UTC)


def item(job_id: int | str, **fields: object) -> dict[str, object]:
    """One job as the search JSON has it (fields seen on 2026-11-18); `fields` override the defaults."""
    return {
        "id": job_id,
        "title": "Desenvolvedor React",
        "subtitle": "Front-end com React e TypeScript",
        "description": "<p>Vaga para React &amp; TypeScript.</p><ul><li>Remoto</li></ul>",
        "jobRequirements": [
            {"id": 1, "jobId": job_id, "description": "Experiência com React.", "mandatory": True},
            {"id": 2, "jobId": job_id, "description": "Inglês intermediário.", "mandatory": False},
        ],
        "company": {"id": 8095, "name": "Acme", "link": "https://acme.example.com/"},
        "companyDisplayName": None,
        "type": "remote",
        "city": None,
        "state": None,
        "createdAt": "2026-11-17T15:28:25.151-03:00",
        "expiresAt": None,
        "expired": False,
        "active": True,
        "integrationSource": None,
        "isExternalLink": True,
        "externalLink": f"https://acme.example.com/vagas/{job_id}",
        **fields,
    }


def page(*items: object) -> str:
    """The search answer: the jobs in `data`, the paging in `meta`."""
    meta = {"total": len(items), "per_page": 50, "current_page": 1, "last_page": 1}
    return json.dumps({"meta": meta, "data": list(items)})


def source(client: httpx.AsyncClient) -> RemotarSource:
    return RemotarSource(client, term_delay=0, clock=lambda: NOW)


def serving(body: str, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body)))


async def test_one_request_per_term_without_cookies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await source(client).fetch_recent(
            '("desenvolvedor react" OR next.js) NOT senior', "Brazil", window_seconds=3600
        )

    assert [r.url.raw_path for r in requests] == [b"/jobs?search=desenvolvedor%20react", b"/jobs?search=next.js"]
    assert {r.url.host for r in requests} == {"api.remotar.com.br"}
    for request in requests:
        assert request.headers["user-agent"] == "linkedin-api-job-alert/0.1"
        assert request.headers["accept"] == "application/json"
        assert "cookie" not in request.headers


async def test_job_is_parsed_and_its_link_rebuilt_from_the_id() -> None:
    body = page(
        item(171836, externalLink="https://evil.example.com/forged"),
        item(2, city="São Paulo", state="SP", type="hybrid", company=None, companyDisplayName="Beta"),
        item(3, city="", state="", type="unknown", company={"name": None}),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("react", "Brazil", window_seconds=3600)

    assert jobs[0].url == "https://remotar.com.br/job/171836"
    assert (jobs[0].key, jobs[0].title) == ("remotar:171836", "Desenvolvedor React")
    assert [j.company for j in jobs] == ["Acme", "Beta", ""]
    assert [j.location for j in jobs] == ["Remoto", "São Paulo, SP (Híbrido)", ""]


async def test_jobs_copied_from_gupy_are_skipped() -> None:
    # The Gupy source already reads them: they would come twice under two keys.
    body = page(item(1, integrationSource="gupy"), item(2, integrationSource="inhire"), item(3))
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("react", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["2", "3"]


async def test_answer_with_only_gupy_jobs_is_zero_jobs_not_a_failure() -> None:
    async with serving(page(item(1, integrationSource="gupy"))) as client:
        assert await source(client).fetch_recent("react", "Brazil", window_seconds=3600) == []


async def test_window_keeps_only_jobs_created_inside_it() -> None:
    body = page(
        item(1, createdAt="2026-11-17T15:28:25.151-03:00"),  # 18:28 UTC
        item(2, createdAt="2026-11-17T14:30:00.000-03:00"),  # 17:30 UTC
    )
    async with serving(body) as client:
        recent = await source(client).fetch_recent("react", "Brazil", window_seconds=3600)
        any_date = await source(client).fetch_recent("react", "Brazil", window_seconds=None)

    assert [j.id for j in recent] == ["1"]
    assert [j.id for j in any_date] == ["1", "2"]


async def test_a_job_found_by_two_terms_comes_once() -> None:
    async with serving(page(item(1), item(2))) as client:
        jobs = await source(client).fetch_recent("react OR next.js", "Brazil", window_seconds=3600)

    assert [j.key for j in jobs] == ["remotar:1", "remotar:2"]


async def test_invalid_jobs_are_skipped_and_valid_ones_remain() -> None:
    body = page(
        item("12a"),
        item(2, createdAt="2026-11-17T15:28:25"),  # no timezone
        item(3, title=None),
        item(4, active=None),
        "not a job",
        item(5),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("react", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["5"]


async def test_answer_where_no_job_is_recognized_is_a_failure() -> None:
    # A renamed field makes every item invalid: that is a layout change, not "no new jobs".
    async with serving(page(item(1, createdAt=None), item(2, createdAt=None))) as client:
        with pytest.raises(SourceUnavailableError, match="no job recognized"):
            await source(client).fetch_recent("react", "Brazil", window_seconds=3600)


async def test_answer_where_no_job_has_a_description_is_a_failure() -> None:
    # The description renamed or gone: every job would be scored without its text, so it is a layout change.
    async with serving(page(item(1, description=None), item(2, description="<p> </p>"))) as client:
        with pytest.raises(SourceUnavailableError, match="no job description"):
            await source(client).fetch_recent("react", "Brazil", window_seconds=3600)


async def test_block_answer_opens_the_circuit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remotar = source(client)
        with pytest.raises(SourceUnavailableError) as blocked:
            await remotar.fetch_recent("react OR next.js", "Brazil", window_seconds=3600)
        assert blocked.value.blocked
        assert blocked.value.retry_after == 120
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await remotar.fetch_recent("react", "Brazil", window_seconds=3600)

    assert len(requests) == 1


async def test_forbidden_is_a_block_too() -> None:
    async with serving("", status=403) as client:
        with pytest.raises(SourceUnavailableError, match="blocked") as blocked:
            await source(client).fetch_recent("react", "Brazil", window_seconds=3600)

    assert blocked.value.blocked
    assert blocked.value.retry_after is None


async def test_server_error_is_a_failure_not_a_block() -> None:
    async with serving("", status=502) as client:
        remotar = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="status 502") as failed:
                await remotar.fetch_recent("react", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await remotar.fetch_recent("react", "Brazil", window_seconds=3600)


async def test_network_error_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceUnavailableError, match="network error: ConnectTimeout") as failed:
            await source(client).fetch_recent("react", "Brazil", window_seconds=3600)

    assert not failed.value.blocked


@pytest.mark.parametrize(
    "body",
    ["<html><body><h1>Checking your browser</h1></body></html>", json.dumps({"meta": {}, "jobs": []})],
    ids=["challenge-page", "no-job-list"],
)
async def test_answer_without_the_job_list_is_a_failure(body: str) -> None:
    async with serving(body) as client:
        remotar = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="layout changed") as failed:
                await remotar.fetch_recent("react", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await remotar.fetch_recent("react", "Brazil", window_seconds=3600)


async def test_empty_search_is_zero_jobs_not_a_failure() -> None:
    async with serving(page()) as client:
        assert await source(client).fetch_recent("react", "Brazil", window_seconds=3600) == []


async def test_details_come_from_the_search_without_a_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=page(
                item(1),
                item(2, jobRequirements=[]),
                item(3, description=None, jobRequirements=[]),
                item(4, expired=True),
                item(5, active=False),
                item(6, expiresAt="2026-11-17T15:00:00.000-03:00"),  # 18:00 UTC, before NOW
                item(7, expiresAt="2026-12-04T12:54:44.000-03:00"),
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remotar = source(client)
        jobs = await remotar.fetch_recent("react", "Brazil", window_seconds=3600)
        details = [await remotar.fetch_details(job) for job in jobs]
        with pytest.raises(SourceUnavailableError) as missing:
            await remotar.fetch_details(Job(id="99", title="Not searched", source="remotar"))

    assert details[0].description == (
        "Vaga para React & TypeScript. Remoto Requisitos: Experiência com React.; Inglês intermediário."
    )
    assert details[1].description == "Vaga para React & TypeScript. Remoto"
    # Scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
    assert details[2].description == "Desenvolvedor React"
    assert [d.closed for d in details] == [False, False, False, True, True, True, False]
    assert not missing.value.blocked
    assert len(requests) == 1
