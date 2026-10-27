import httpx
import pytest

from app.errors import SourceUnavailableError
from app.sources.linkedin_guest import LinkedInGuestSource, parse_cards
from tests.helpers import card


def test_parse_cards_reads_fields_and_skips_invalid_cards() -> None:
    jobs, cards = parse_cards(card("4408463983", company="Acme &amp; Co") + card("4408463984", title=""))
    assert cards == 2
    assert len(jobs) == 1
    assert jobs[0].id == "4408463983"
    assert jobs[0].title == "Python Developer"
    assert jobs[0].company == "Acme & Co"
    assert jobs[0].location == "São Paulo, Brazil"
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4408463983/"


async def test_reads_every_page_of_the_window_until_a_short_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = int(request.url.params["start"])
        size = 10 if start == 0 else 3
        return httpx.Response(200, text="".join(card(str(5000 + start + i)) for i in range(size)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await LinkedInGuestSource(client, page_delay=0).fetch_recent("python", "Brazil")

    assert len(jobs) == 13
    assert [r.url.params["start"] for r in requests] == ["0", "10"]
    params = requests[0].url.params
    assert (params["f_AL"], params["f_TPR"], params["location"]) == ("true", "r3600", "Brazil")
    assert "cookie" not in requests[0].headers


async def test_block_answer_opens_the_circuit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = LinkedInGuestSource(client, page_delay=0)
        with pytest.raises(SourceUnavailableError) as blocked:
            await source.fetch_recent("python", "Brazil")
        assert blocked.value.blocked
        assert blocked.value.retry_after == 120
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await source.fetch_recent("python", "Brazil")

    assert len(requests) == 1
