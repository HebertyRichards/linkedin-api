from datetime import UTC, datetime

import httpx
import pytest

from app.errors import SourceUnavailableError
from app.schemas.job import Job
from app.sources.workable import WorkableSource

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
BLACKBIRD_URL = "https://jobs.workable.com/view/jr513vzR29GbUbPCaRd2Dh"


def item(code: str, **fields: object) -> dict[str, object]:
    """One job as the search JSON has it (fields seen on 2026-11-18); `fields` override the defaults."""
    return {
        "id": "f1d5ae4a-9473-4bcd-a6cc-2bcac7bd19fb",
        "title": "Desenvolvedor Python",
        "description": "<p>Vaga para <strong>Python</strong> &amp; FastAPI.</p>",
        "requirementsSection": "<ul><li>Django</li><li>SQL</li></ul>",
        "benefitsSection": "",
        "employmentType": "Full-time",
        "url": f"https://jobs.workable.com/view/{code}/desenvolvedor-python-in-s%C3%A3o-paulo-at-acme",
        "language": "pt",
        "locations": ["TELECOMMUTE", "São Paulo, State of São Paulo, Brazil"],
        "location": {"city": "São Paulo", "subregion": "State of São Paulo", "countryName": "Brazil"},
        "created": "2026-11-18T11:30:00.000Z",
        "updated": "2026-11-18T11:30:00.000Z",
        "company": {"title": "Acme"},
        "workplace": "remote",
        "state": "published",
        **fields,
    }


def answer(*items: object) -> dict[str, object]:
    return {"title": "Workable", "totalSize": len(items), "nextPageToken": "x", "jobs": list(items)}


def source(client: httpx.AsyncClient) -> WorkableSource:
    return WorkableSource(client, term_delay=0, clock=lambda: NOW)


def serving(body: object, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)))


async def test_one_request_per_term_without_cookies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await source(client).fetch_recent('("full stack" OR next.js) NOT senior', "Portugal", window_seconds=3600)
        await source(client).fetch_recent("python", "Brazil", window_seconds=None)

    assert [dict(r.url.params) for r in requests] == [
        {"query": "full stack", "location": "Brazil", "day_range": "1"},
        {"query": "next.js", "location": "Brazil", "day_range": "1"},
        {"query": "python", "location": "Brazil"},  # any date: no day filter
    ]
    assert {(r.url.scheme, r.url.host, r.url.path) for r in requests} == {
        ("https", "jobs.workable.com", "/api/v1/jobs")
    }
    for request in requests:
        assert request.headers["user-agent"] == "linkedin-api-job-alert/0.1"
        assert request.headers["accept"] == "application/json"
        assert "cookie" not in request.headers


async def test_job_is_parsed_and_link_rebuilt_from_the_code() -> None:
    body = answer(
        item("jr513vzR29GbUbPCaRd2Dh", company={"title": "Blackbird"}, workplace="on_site"),
        item("abc123", location={"city": "", "subregion": None, "countryName": "Brazil"}),
        item(
            "def456",
            location={"city": "Barueri", "subregion": "São Paulo", "countryName": "Brazil"},
            workplace="hybrid",
        ),
        item("ghi789", url="https://jobs.workable.com/view/ghi789"),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert [(j.id, j.source) for j in jobs] == [
        ("jr513vzR29GbUbPCaRd2Dh", "workable"),
        ("abc123", "workable"),
        ("def456", "workable"),
        ("ghi789", "workable"),
    ]
    assert jobs[0].url == BLACKBIRD_URL
    assert (jobs[0].title, jobs[0].company) == ("Desenvolvedor Python", "Blackbird")
    assert [j.location for j in jobs] == [
        "São Paulo, State of São Paulo (Presencial)",
        "Remoto",
        "Barueri, São Paulo (Híbrido)",
        "São Paulo, State of São Paulo (Remoto)",
    ]


async def test_link_outside_the_job_page_shape_is_skipped() -> None:
    body = answer(
        item("ok0001"),
        item("x", url="https://evil.com/view/abc123/x"),
        item("x", url="http://jobs.workable.com/view/abc123/x"),
        item("x", url="https://jobs.workable.com.evil.com/view/abc123"),
        item("x", url="https://apply.workable.com/view/abc123"),
        item("x", url="https://jobs.workable.com/company/abc123/jobs-at-acme"),
        item("x", url="https://jobs.workable.com/view/abc"),  # code too short
        item("x", url="https://jobs.workable.com/view/abc-123/x"),
        item("x", url="https://jobs.workable.com/view/abc123?x=1"),
        item("ok0002"),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["ok0001", "ok0002"]


async def test_invalid_jobs_are_skipped_and_valid_ones_remain() -> None:
    body = answer(
        item("aaa111", created="2026-11-18T11:30:00"),  # no timezone
        item("bbb222", title=None),
        item("ccc333", state=None),
        item("ddd444", location={"city": "São Paulo"}),  # no country: the guard below cannot run
        "not a job",
        item("eee555"),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["eee555"]


async def test_job_outside_brazil_is_dropped() -> None:
    lisbon = {"city": "Lisbon", "subregion": "Lisbon", "countryName": "Portugal"}
    async with serving(answer(item("pt0001", location=lisbon), item("br0001"))) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["br0001"]


async def test_window_keeps_only_jobs_created_inside_it() -> None:
    body = answer(item("new001", created="2026-11-18T11:30:00Z"), item("old001", created="2026-11-18T10:30:00.000Z"))
    async with serving(body) as client:
        recent = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)
        any_date = await source(client).fetch_recent("python", "Brazil", window_seconds=None)

    assert [j.id for j in recent] == ["new001"]
    assert [j.id for j in any_date] == ["new001", "old001"]


async def test_a_job_found_by_two_terms_comes_once() -> None:
    async with serving(answer(item("abc001"), item("abc002"))) as client:
        jobs = await source(client).fetch_recent("python OR django", "Brazil", window_seconds=3600)

    assert [j.key for j in jobs] == ["workable:abc001", "workable:abc002"]


async def test_answer_where_no_job_is_recognized_is_a_failure() -> None:
    # A renamed field makes every item invalid: that is a layout change, not "no new jobs".
    async with serving(answer(item("abc001", created=None), item("abc002", created=None))) as client:
        with pytest.raises(SourceUnavailableError, match="no job recognized"):
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)


async def test_answer_where_no_job_has_a_description_is_a_failure() -> None:
    # The description renamed or gone: every job would be scored on its title alone, so it is a layout change.
    async with serving(answer(item("abc001", description=None), item("abc002", description="<p> </p>"))) as client:
        with pytest.raises(SourceUnavailableError, match="no job description"):
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)


@pytest.mark.parametrize("status", [403, 429])
async def test_block_answer_opens_the_circuit(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        workable = source(client)
        with pytest.raises(SourceUnavailableError) as blocked:
            await workable.fetch_recent("python OR django", "Brazil", window_seconds=3600)
        assert blocked.value.blocked
        assert blocked.value.retry_after == 120
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await workable.fetch_recent("python", "Brazil", window_seconds=3600)

    assert len(requests) == 1


async def test_other_status_and_network_error_are_failures() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with serving(answer(), status=500) as client:
        workable = source(client)
        with pytest.raises(SourceUnavailableError, match="status 500") as failed:
            await workable.fetch_recent("python", "Brazil", window_seconds=3600)
        assert not failed.value.blocked
    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        workable = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="network error: ConnectError") as failed:
                await workable.fetch_recent("python", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await workable.fetch_recent("python", "Brazil", window_seconds=3600)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html><body><h1>Checking your browser</h1></body></html>"),
        httpx.Response(200, json={"title": "Workable", "results": []}),
        httpx.Response(200, json=[]),
    ],
    ids=["challenge-page", "no-job-list", "not-an-object"],
)
async def test_answer_without_the_job_list_is_a_failure(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        workable = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="layout changed") as failed:
                await workable.fetch_recent("python", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await workable.fetch_recent("python", "Brazil", window_seconds=3600)


async def test_empty_search_is_zero_jobs_not_a_failure() -> None:
    async with serving(answer()) as client:
        assert await source(client).fetch_recent("python", "Brazil", window_seconds=3600) == []


async def test_details_come_from_the_search_without_a_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=answer(
                item("abc001"),
                item("abc002", employmentType="Contract", state="closed", requirementsSection=None),
                item("abc003", description="  "),
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        workable = source(client)
        published, closed_job, no_description = await workable.fetch_recent("python", "Brazil", window_seconds=3600)
        details = await workable.fetch_details(published)
        closed = await workable.fetch_details(closed_job)
        title_only = await workable.fetch_details(no_description)
        with pytest.raises(SourceUnavailableError) as missing:
            await workable.fetch_details(Job(id="zzz999", title="Not searched", source="workable"))

    # Requirements appended after the description.
    assert (details.description, details.employment_type, details.closed) == (
        "Vaga para Python & FastAPI. Django SQL",
        "Full-time",
        False,
    )
    assert (closed.description, closed.employment_type, closed.closed) == (
        "Vaga para Python & FastAPI.",
        "Contract",
        True,
    )
    # Scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
    assert title_only.description == "Desenvolvedor Python"
    assert not missing.value.blocked
    assert len(requests) == 1
