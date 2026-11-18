import pytest
from pydantic import ValidationError

from app.schemas.job import Job, JobDetails, Source
from app.sources.base import DetailsCache
from app.utils.html_text import html_to_text


@pytest.mark.parametrize(
    ("source", "job_id", "url"),
    [
        ("linkedin", "4490181385", "https://www.linkedin.com/jobs/view/4490181385/"),
        ("remotar", "171836", "https://remotar.com.br/job/171836"),
        (
            "himalayas",
            "clash/desenvolvedor-a-fullstack",
            "https://himalayas.app/companies/clash/jobs/desenvolvedor-a-fullstack",
        ),
        ("workable", "jr513vzR29GbUbPCaRd2Dh", "https://jobs.workable.com/view/jr513vzR29GbUbPCaRd2Dh"),
    ],
)
def test_each_source_builds_its_link_from_the_id(source: Source, job_id: str, url: str) -> None:
    assert Job(id=job_id, title="x", source=source).url == url


@pytest.mark.parametrize(
    ("source", "job_id"),
    [
        ("linkedin", "clash/dev"),
        ("remotar", "12a"),
        ("himalayas", "clash"),
        ("himalayas", "../evil/x"),
        ("himalayas", "Clash/dev"),
        ("workable", "abc/def"),
        ("workable", "x" * 41),
    ],
)
def test_an_id_that_does_not_fit_its_source_is_refused(source: Source, job_id: str) -> None:
    with pytest.raises(ValidationError):
        Job(id=job_id, title="x", source=source)


def test_html_to_text_keeps_words_apart_and_decodes_entities() -> None:
    fragment = "<p>Vaga <strong>Python</strong>&nbsp;&amp; Go</p><ul><li>SQL</li><li>Docker</li></ul>"
    assert html_to_text(fragment) == "Vaga Python & Go SQL Docker"


def test_details_cache_holds_a_whole_digest_of_the_widest_source() -> None:
    # A digest reads every job first and asks the pages afterwards: Remotar's 3 searches of 4 alternatives,
    # 50 jobs each, must all still be there.
    cache = DetailsCache()
    jobs = [Job(id=str(n), title="Dev", source="remotar") for n in range(3 * 4 * 50)]
    for job in jobs:
        cache.keep(job, JobDetails(description="text"))
    assert cache.get(jobs[0]).description == "text"
