import json
from datetime import UTC, datetime

import httpx
import pytest

from app.errors import SourceUnavailableError
from app.schemas.job import Job
from app.sources.gupy_portal import GupyPortalSource
from app.utils.search_query import search_terms

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
GRUPOMATER_URL = "https://grupomater.gupy.io/job/eyJqb2JJZCI6MTI4NjI1NDAsInNvdXJjZSI6Imd1cHlfcG9ydGFsIn0="
# The three searches in production on 2026-11-17.
FULL_STACK = (
    '("desenvolvedor full stack" OR "full stack developer" OR fullstack) '
    "NOT senior NOT sênior NOT especialista NOT assistente NOT consultor NOT estágio"
)
FRONT_END = (
    '("desenvolvedor front-end" OR "desenvolvedor react" OR "next.js" OR "frontend developer") '
    "NOT senior NOT sênior NOT especialista NOT assistente NOT consultor NOT estágio"
)
BACK_END = (
    '("desenvolvedor back-end" OR "desenvolvedor python" OR "desenvolvedor node" OR "desenvolvedor java" '
    'OR "desenvolvedor php") NOT senior NOT sênior NOT especialista NOT assistente NOT consultor NOT estágio'
)


def item(job_id: int | str, **fields: object) -> dict[str, object]:
    """One job as the search JSON has it (fields seen on 2026-11-17); `fields` override the defaults."""
    return {
        "id": job_id,
        "name": "Desenvolvedor Python",
        "careerPageName": "Acme",
        "city": "São Paulo",
        "state": "São Paulo",
        "workplaceType": "remote",
        "type": "vacancy_type_effective",
        "publishedDate": "2026-11-16T11:30:00.000Z",
        "applicationDeadline": "2027-01-12T00:00:00.000Z",
        "jobUrl": f"https://acme.gupy.io/job/token{job_id}?jobBoardSource=gupy_portal",
        "description": "Vaga para Python &amp; FastAPI.",
        **fields,
    }


def page(*items: object) -> str:
    """A search page with the jobs embedded in `__NEXT_DATA__`, as Next.js renders it."""
    data = {"props": {"pageProps": {"initialJobList": {"data": list(items), "pagination": {"total": len(items)}}}}}
    return (
        '<html><body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json" nonce="n">'
        f"{json.dumps(data)}</script></body></html>"
    )


def source(client: httpx.AsyncClient) -> GupyPortalSource:
    return GupyPortalSource(client, term_delay=0, clock=lambda: NOW)


def serving(body: str, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body)))


@pytest.mark.parametrize(
    ("keywords", "terms"),
    [
        (FULL_STACK, ["desenvolvedor full stack", "full stack developer", "fullstack"]),
        (FRONT_END, ["desenvolvedor front-end", "desenvolvedor react", "next.js", "frontend developer"]),
        (
            BACK_END,
            [
                "desenvolvedor back-end",
                "desenvolvedor python",
                "desenvolvedor node",
                "desenvolvedor java",
                "desenvolvedor php",
            ],
        ),
        ("python", ["python"]),
    ],
    ids=["full-stack", "front-end", "back-end", "plain"],
)
def test_search_terms_split_a_linkedin_search(keywords: str, terms: list[str]) -> None:
    assert search_terms(keywords) == terms


async def test_one_request_per_term_without_cookies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await source(client).fetch_recent('("full stack" OR next.js) NOT senior', "Brazil", window_seconds=3600)

    assert [r.url.raw_path for r in requests] == [b"/job-search/term=full%20stack", b"/job-search/term=next.js"]
    assert {r.url.host for r in requests} == {"portal.gupy.io"}
    for request in requests:
        assert request.headers["user-agent"] == "linkedin-api-job-alert/0.1"
        assert "cookie" not in request.headers


async def test_window_keeps_only_jobs_published_inside_it() -> None:
    body = page(item(1, publishedDate="2026-11-16T11:30:00.000Z"), item(2, publishedDate="2026-11-16T10:30:00Z"))
    async with serving(body) as client:
        recent = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)
        any_date = await source(client).fetch_recent("python", "Brazil", window_seconds=None)

    assert [j.id for j in recent] == ["1"]
    assert [j.id for j in any_date] == ["1", "2"]


@pytest.mark.parametrize(
    ("location", "kept"),
    [
        ("Brazil", ["1", "4", "5"]),
        ("Brasil", ["1", "4", "5"]),
        ("São Paulo", ["1", "4"]),  # the state of job 1 (and its city)
        ("goiania, Brazil", ["4", "5"]),  # the first part, without accents or case
        ("Lisbon", ["2", "4"]),
    ],
)
async def test_only_the_jobs_in_job_location_are_kept(location: str, kept: list[str]) -> None:
    body = page(
        item(1),
        item(2, city="Lisbon", state=""),  # abroad: a city with no state, as Gupy lists it
        item(3, city="Porto", state=""),
        item(4, city="", state="", workplaceType="remote"),  # no place given: kept, the score reads the text
        item(5, city="Goiânia", state="GOIÁS"),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", location, window_seconds=None)

    assert [j.id for j in jobs] == kept


async def test_a_job_found_by_two_terms_comes_once() -> None:
    async with serving(page(item(1), item(2))) as client:
        jobs = await source(client).fetch_recent("python OR django", "Brazil", window_seconds=3600)

    assert [j.key for j in jobs] == ["gupy:1", "gupy:2"]


async def test_invalid_jobs_are_skipped_and_valid_ones_remain() -> None:
    body = page(
        item("12a"),
        item(2, jobUrl="https://evil.com/job/x"),
        item(3, jobUrl="https://a.b.gupy.io/job/x"),
        item(4, jobUrl="http://acme.gupy.io/job/x"),
        item(5, publishedDate="2026-11-16T11:30:00"),  # no timezone
        item(6, name=None),
        "not a job",
        item(7),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert [j.id for j in jobs] == ["7"]


async def test_page_where_no_job_is_recognized_is_a_failure() -> None:
    # A renamed field makes every item invalid: that is a layout change, not "no new jobs".
    async with serving(page(item(1, publishedDate=None), item(2, publishedDate=None))) as client:
        with pytest.raises(SourceUnavailableError, match="no job recognized"):
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)


async def test_page_where_no_job_has_a_description_is_a_failure() -> None:
    # The description renamed or gone: every job would be scored on its title alone, so it is a layout change.
    async with serving(page(item(1, description=None), item(2, description=""))) as client:
        with pytest.raises(SourceUnavailableError, match="no job description"):
            await source(client).fetch_recent("python", "Brazil", window_seconds=3600)


async def test_job_link_is_rebuilt_from_id_and_career_site() -> None:
    body = page(
        item(12862540, jobUrl="https://GrupoMater.gupy.io/job/forged?jobBoardSource=gupy_portal"),
        item(2, city="", state="", workplaceType="remote"),
    )
    async with serving(body) as client:
        jobs = await source(client).fetch_recent("python", "Brazil", window_seconds=3600)

    assert jobs[0].url == GRUPOMATER_URL
    assert Job(id="12862540", title="x", source="gupy", site="grupomater").url == GRUPOMATER_URL
    assert [j.location for j in jobs] == ["São Paulo, São Paulo (Remoto)", "Remoto"]


async def test_block_answer_opens_the_circuit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gupy = source(client)
        with pytest.raises(SourceUnavailableError) as blocked:
            await gupy.fetch_recent("python OR django", "Brazil", window_seconds=3600)
        assert blocked.value.blocked
        assert blocked.value.retry_after == 120
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await gupy.fetch_recent("python", "Brazil", window_seconds=3600)

    assert len(requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        "<html><body><h1>Checking your browser</h1></body></html>",
        '<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {}}}</script>',
    ],
    ids=["challenge-page", "no-job-list"],
)
async def test_page_without_the_job_list_is_a_failure(body: str) -> None:
    async with serving(body) as client:
        gupy = source(client)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="layout changed") as failed:
                await gupy.fetch_recent("python", "Brazil", window_seconds=3600)
            assert not failed.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await gupy.fetch_recent("python", "Brazil", window_seconds=3600)


async def test_empty_search_is_zero_jobs_not_a_failure() -> None:
    async with serving(page()) as client:
        assert await source(client).fetch_recent("python", "Brazil", window_seconds=3600) == []


async def test_details_come_from_the_search_without_a_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=page(
                item(1),
                item(2, type="vacancy_legal_entity", applicationDeadline="2026-11-15T00:00:00.000Z"),
                item(3, description="  "),
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gupy = source(client)
        effective, legal_entity, no_description = await gupy.fetch_recent("python", "Brazil", window_seconds=3600)
        details = await gupy.fetch_details(effective)
        closed = await gupy.fetch_details(legal_entity)
        title_only = await gupy.fetch_details(no_description)
        with pytest.raises(SourceUnavailableError) as missing:
            await gupy.fetch_details(Job(id="99", title="Not searched", source="gupy", site="acme"))

    assert (details.description, details.employment_type, details.closed) == (
        "Vaga para Python & FastAPI.",
        "Efetivo (CLT)",
        False,
    )
    assert (closed.employment_type, closed.closed) == ("Pessoa jurídica (PJ)", True)
    # Scored on its title (low, so dropped) instead of failing its page until it is given up unseen.
    assert title_only.description == "Desenvolvedor Python"
    assert not missing.value.blocked
    assert len(requests) == 1
