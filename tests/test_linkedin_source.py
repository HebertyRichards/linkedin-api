import httpx
import pytest

from app.errors import SourceUnavailableError
from app.sources.linkedin_guest import LinkedInGuestSource, parse_cards, parse_details
from tests.helpers import card, job


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
        jobs = await LinkedInGuestSource(client, page_delay=0).fetch_recent("python", "Brazil", window_seconds=3600)

    assert len(jobs) == 13
    assert [r.url.params["start"] for r in requests] == ["0", "10"]
    params = requests[0].url.params
    assert (params["f_TPR"], params["location"]) == ("r3600", "Brazil")
    assert "f_AL" not in params  # every job, Easy Apply or not
    assert "cookie" not in requests[0].headers


async def test_no_window_searches_jobs_of_any_date() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=card("5000"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await LinkedInGuestSource(client, page_delay=0).fetch_recent("python", "Brazil", window_seconds=None)

    assert "f_TPR" not in requests[0].url.params
    assert "f_AL" not in requests[0].url.params


async def test_block_answer_opens_the_circuit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = LinkedInGuestSource(client, page_delay=0)
        with pytest.raises(SourceUnavailableError) as blocked:
            await source.fetch_recent("python", "Brazil", window_seconds=3600)
        assert blocked.value.blocked
        assert blocked.value.retry_after == 120
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await source.fetch_recent("python", "Brazil", window_seconds=3600)

    assert len(requests) == 1


async def test_empty_search_is_zero_jobs_not_a_failure() -> None:
    # The body LinkedIn returned for a search with no results on 2026-10-29.
    empty = "<!DOCTYPE html>\n\n<!---->  "
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=empty))) as client:
        source = LinkedInGuestSource(client, page_delay=0)
        assert await source.fetch_recent("python", "Brazil", window_seconds=3600) == []


@pytest.mark.parametrize(
    "body",
    ["<html><body><h1>Let's do a quick security check</h1></body></html>", card("4408463983", title="")],
    ids=["challenge-page", "only-invalid-cards"],
)
async def test_page_without_a_usable_card_is_a_failure(body: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as client:
        source = LinkedInGuestSource(client, page_delay=0)
        for _ in range(3):
            with pytest.raises(SourceUnavailableError, match="markup changed"):
                await source.fetch_recent("python", "Brazil", window_seconds=3600)
        # Three failures open the circuit, so a broken page is not requested every cycle.
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await source.fetch_recent("python", "Brazil", window_seconds=3600)


# The job page markup observed on 2026-11-12 (trimmed): nested divs inside the description.
JOB_PAGE = """
<div class="description__text description__text--rich">
  <section class="show-more-less-html" data-max-lines="5">
    <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5
        relative overflow-hidden">
      We are hiring a <strong>Python</strong> developer.<br><br>
      <div><p>Requirements</p><ul><li>FastAPI</li><li>English B2</li></ul></div>
      After the nested block.
    </div>
  </section>
</div>
<ul class="description__job-criteria-list">
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">
      Seniority level
    </h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">
      Entry level
    </span>
  </li>
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Employment type</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Full-time</span>
  </li>
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Industries</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Software</span>
  </li>
</ul>
<button class="show-more-less-html__button">Show more</button>
"""


def test_job_page_gives_description_and_criteria() -> None:
    details = parse_details(JOB_PAGE)
    assert details.description == (
        "We are hiring a Python developer. Requirements FastAPI English B2 After the nested block."
    )
    assert (details.seniority, details.employment_type) == ("Entry level", "Full-time")


def test_job_page_without_description_is_a_failure() -> None:
    with pytest.raises(SourceUnavailableError, match="without a description"):
        parse_details("<html><body><h1>Let's do a quick security check</h1></body></html>")


async def test_job_page_block_opens_the_circuit_for_the_search_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = LinkedInGuestSource(client, page_delay=0)
        with pytest.raises(SourceUnavailableError) as blocked:
            await source.fetch_details(job(1))
        assert blocked.value.blocked
        with pytest.raises(SourceUnavailableError, match="circuit open"):
            await source.fetch_recent("python", "Brazil", window_seconds=3600)


def test_a_closed_job_page_says_so() -> None:
    closed = (
        '<figure class="closed-job closed-job__flavor topcard__flavor-row">'
        '<figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption></figure>'
        + JOB_PAGE
    )
    assert parse_details(closed).closed is True
    assert parse_details(JOB_PAGE).closed is False


async def test_a_removed_job_page_counts_as_closed() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        details = await LinkedInGuestSource(client, page_delay=0).fetch_details(job(1))
    assert details.closed is True
