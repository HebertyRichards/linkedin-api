import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.schemas.resume import Resume, load_resume, parse_resume
from app.services.assessor import JobAssessor, technologies, without_versions
from app.services.llm import ChatClient
from tests.helpers import DETAILS, RESUME_YAML, job, make_settings

Handler = Callable[[httpx.Request], httpx.Response]
LLM = {"llm_api_key": "gsk_test_key", "resume_file": "curriculo.yaml"}


def reply(content: str) -> httpx.Response:
    """A chat-completions answer in the OpenAI shape that Groq returns."""
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


GOOD = json.dumps({"score": 82, "workplace": "hybrid", "reasons": "Stack parecida.", "gaps": ["Kubernetes"]})


@pytest.fixture
def resume(tmp_path: Path) -> Resume:
    path = tmp_path / "curriculo.yaml"
    path.write_text(RESUME_YAML, encoding="utf-8")
    return load_resume(path)


def assessor_for(handler: Handler, resume: Resume) -> tuple[JobAssessor, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chat = ChatClient(client, make_settings(**LLM), model="openai/gpt-oss-20b", retry_base_delay=0)
    assessor = JobAssessor(chat, resume, today=lambda: date(2026, 11, 12))
    return assessor, client


async def test_scores_a_job_without_sending_contact_data(resume: Resume) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return reply(GOOD)

    assessor, client = assessor_for(handler, resume)
    async with client:
        result = await assessor.assess(job(1), DETAILS)

    assert result is not None
    assert (result.score, result.workplace, result.gaps) == (82, "hybrid", ("Kubernetes",))
    request = requests[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer gsk_test_key"
    body = json.loads(request.content)
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["reasoning_effort"] == "low"
    assert body["response_format"]["json_schema"]["strict"] is True
    prompt = body["messages"][1]["content"]
    assert "Pipeline de vendas diárias em Go." in prompt and "Buscamos pessoa de dados com Go." in prompt
    assert "atual (2026-11)" in prompt
    for contact in ("Ana Exemplo", "00000-0000", "ana@example.com"):
        assert contact not in prompt


async def test_a_scored_job_is_not_sent_to_the_llm_again(resume: Resume) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return reply(GOOD)

    assessor, client = assessor_for(handler, resume)
    async with client:
        first = await assessor.assess(job(1), DETAILS)
        again = await assessor.assess(job(1), DETAILS)

    assert first == again == assessor.cached(job(1))
    assert calls == 1


async def test_quota_window_stops_asking_until_it_passes(resume: Resume) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "3600"})

    assessor, client = assessor_for(handler, resume)
    async with client:
        assert await assessor.assess(job(1), DETAILS) is None
        assert await assessor.assess(job(2), DETAILS) is None

    assert calls == 1  # no retry inside a quota window, and the second job never calls


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"score": 150, "workplace": "remote", "reasons": "x", "gaps": []})],
    ids=["not-json", "score-out-of-range"],
)
async def test_unusable_reply_is_no_score(resume: Resume, content: str) -> None:
    assessor, client = assessor_for(lambda _: reply(content), resume)
    async with client:
        assert await assessor.assess(job(1), DETAILS) is None


async def test_refused_request_is_logged_without_the_key(resume: Resume, caplog: pytest.LogCaptureFixture) -> None:
    assessor, client = assessor_for(
        lambda _: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}), resume
    )
    async with client:
        assert await assessor.assess(job(1), DETAILS) is None

    assert "status 401" in caplog.text
    assert "gsk_test_key" not in caplog.text


def test_llm_output_is_bounded() -> None:
    from app.schemas.job import Assessment

    long = Assessment.model_validate(
        {"score": 50, "workplace": "unknown", "reasons": "x " * 500, "gaps": [f"gap {n}" for n in range(9)]}
    )
    assert len(long.reasons) <= 200
    assert long.reasons.endswith("x…")  # cut between words, never inside one
    assert len(long.gaps) == 5


def test_broken_resume_names_the_problem_not_the_content(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "curriculo.yaml"
    path.write_text("contato: {telefone: '(00) 12345-6789'}\ntitulo: Dev\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="the resume is missing or has invalid fields") as error:
        load_resume(path)
    assert "12345" not in str(error.value)

    path.write_text("titulo: [unclosed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid YAML"):
        load_resume(path)
    with pytest.raises(RuntimeError, match="RESUME_FILE could not be read"):
        load_resume(tmp_path / "missing.yaml")


def test_resume_from_a_variable_matches_the_file(tmp_path: Path, resume: Resume) -> None:
    # RESUME_YAML on Railway holds the same text as the local file.
    assert parse_resume(RESUME_YAML) == resume


async def test_per_minute_limit_is_waited_out_not_taken_for_the_daily_quota(
    resume: Resume, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Groq's token-per-minute window answers 429 with a Retry-After of up to ~60 s (seen live: 1 to 59 s).
    waits: list[float] = []

    async def no_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr("app.utils.retry.asyncio.sleep", no_sleep)
    answers = iter([httpx.Response(429, headers={"retry-after": "45"}), reply(GOOD), reply(GOOD)])
    assessor, client = assessor_for(lambda _: next(answers), resume)
    async with client:
        first = await assessor.assess(job(1), DETAILS)
        second = await assessor.assess(job(2), DETAILS)

    assert first is not None and second is not None  # the rest of the batch is still scored
    assert waits == [45.0]


def test_versions_of_known_technologies_are_dropped_before_the_llm(resume: Resume) -> None:
    names = technologies(resume)
    text = "Go 1.22, kafka 3.x, Rust 1.75+ e Go 5 anos de experiência; Kotlin 2 e MySQL 8."

    assert without_versions(text, names) == "Go, kafka, Rust e Go 5 anos de experiência; Kotlin 2 e MySQL 8."
    assert without_versions("Java 17", {"Java"}) == "Java"
    assert without_versions("MySQL 8", {"SQL"}) == "MySQL 8"  # a name inside another word is not touched


async def test_the_prompt_has_no_version_gap_to_penalize(resume: Resume) -> None:
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][1]["content"])
        return reply(GOOD)

    details = DETAILS.model_copy(update={"description": "Requisitos: Go 1.22 e Kafka 3.6."})
    assessor, client = assessor_for(handler, resume)
    async with client:
        await assessor.assess(job(1), details)

    assert "Requisitos: Go e Kafka." in prompts[0]


@pytest.mark.parametrize(
    "text", ["Java 3 a 5 anos", "Java 2-3 anos", "Java 3 ou mais anos", "Java 5+ years", "Java 18 meses"]
)
def test_years_of_experience_are_never_taken_for_a_version(text: str) -> None:
    assert without_versions(text, {"Java"}) == text


def test_a_number_that_is_part_of_the_name_stays() -> None:
    assert "S3" in technologies(parse_resume(RESUME_YAML.replace("[Go, Kafka, Rust]", "[Go, Kafka, Rust, S3]")))
    assert without_versions("Java 17 e S3", {"Java", "S3"}) == "Java e S3"
