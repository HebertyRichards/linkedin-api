import json
from datetime import date

import httpx
import pytest

from app.schemas.resume import Contact, Resume, TailoredResume, parse_contact, parse_resume
from app.services.resume_pdf import _latin1, render_pdf
from app.services.tailor import (
    ResumeTailor,
    TailorChoice,
    _choices_text,
    _filename,
    build_tailored,
    safe_summary,
)
from tests.helpers import DETAILS, RESUME_YAML, build_chat, job

TODAY = date(2026, 11, 12)
LLM = {"llm_api_key": "gsk_test_key", "resume_file": "curriculo.yaml"}


@pytest.fixture
def resume() -> Resume:
    return parse_resume(RESUME_YAML)


@pytest.fixture
def contact() -> Contact:
    return parse_contact(RESUME_YAML)


def choice(**overrides: object) -> TailorChoice:
    values: dict[str, object] = {
        "resumo": "Engenheira de dados com Go e Kafka.",
        "destaques": ["padaria-fila"],
        "projeto": "horta",
        "habilidades": ["Kafka"],
        **overrides,
    }
    return TailorChoice.model_validate(values)


def tailored(resume: Resume, description: str = "Vaga com Go.", **overrides: object) -> TailoredResume:
    return build_tailored(resume, choice(**overrides), description, resume.prompt_text(TODAY))


def test_chosen_highlights_come_first_and_unknown_ids_are_ignored(resume: Resume) -> None:
    result = tailored(resume, destaques=["inventado", "padaria-fila"])

    assert [item.id for item in result.experiencias[0].destaques] == ["padaria-fila", "padaria-etl"]
    # Companies, roles and dates come from the base, never from the LLM.
    assert result.experiencias[0].empresa == "Padaria Modelo"


def test_the_fixed_project_stays_and_one_more_is_picked(resume: Resume) -> None:
    assert [p.id for p in tailored(resume).projetos] == ["clima", "horta"]
    # An id that does not exist falls back to the first project that can be chosen.
    assert [p.id for p in tailored(resume, projeto="nao-existe").projetos] == ["clima", "feira"]


def test_with_every_project_fixed_all_of_them_stay(resume: Resume) -> None:
    fixed = resume.model_copy(update={"projetos": [p.model_copy(update={"fixo": True}) for p in resume.projetos]})

    result = build_tailored(fixed, choice(projeto=""), "Vaga com Go.", fixed.prompt_text(TODAY))

    assert [p.id for p in result.projetos] == ["clima", "feira", "horta"]
    assert 'nenhum (responda projeto "")' in _choices_text(fixed)


def test_skills_are_only_the_resume_ones_and_on_demand_only_when_the_job_names_them(resume: Resume) -> None:
    plain = tailored(resume, habilidades=["Kafka", "Kubernetes"])
    assert plain.habilidades == ["Kafka", "Go", "Rust"]

    asked = tailored(resume, description="Usamos Airflow e Go.", habilidades=["airflow"])
    assert asked.habilidades == ["Airflow", "Go", "Kafka", "Rust"]


@pytest.mark.parametrize(
    "candidate",
    [
        "Engenheira de dados com Go e Kubernetes.",  # a technology the resume does not have
        "Engenheira de dados pleno com Go.",  # a seniority level
        "Pipelines com Go/Kubernetes.",  # an unknown term joined to a known one
        "Engenheira de dados. Spark e Go.",  # a term copied from the job, at a sentence start
        "",
        "x" * 700,
    ],
)
def test_a_summary_that_is_not_safe_falls_back_to_the_base_one(resume: Resume, candidate: str) -> None:
    job_description = "Vaga com Spark e Go."
    assert safe_summary(candidate, resume, resume.prompt_text(TODAY), job_description) == resume.resumo


def test_plain_words_at_a_sentence_start_and_joined_known_terms_are_kept(resume: Resume) -> None:
    candidate = "Atuei com Go/Kafka em pipelines. Otimizei filas de pedidos."
    assert safe_summary(candidate, resume, resume.prompt_text(TODAY), "Vaga com Spark e Go.") == candidate


def test_a_summary_with_a_word_of_the_job_title_the_resume_lacks_falls_back(resume: Resume) -> None:
    # "plataforma de trading" read as experience the candidate does not have (seen on 2026-11-18).
    known = resume.prompt_text(TODAY)
    title = "Full Stack Engineer — Trading Platform (AI-Native)"
    borrowed = "Atuei com Go em plataforma de trading."
    assert safe_summary(borrowed, resume, known, "Python and React.", title) == resume.resumo
    assert safe_summary("Atuei com Go em pipelines.", resume, known, "Python and React.", title) != resume.resumo


def test_a_safe_summary_is_kept(resume: Resume) -> None:
    candidate = "Engenheira de dados  com Go e Kafka."
    assert safe_summary(candidate, resume, resume.prompt_text(TODAY)) == "Engenheira de dados com Go e Kafka."


def test_filename_is_plain_ascii() -> None:
    assert (
        _filename(job(1).model_copy(update={"company": "Café & Cia / São Paulo"})) == "Curriculo-Cafe-Cia-Sao-Paulo.pdf"
    )
    assert _filename(job(1).model_copy(update={"company": "!!!"})) == "Curriculo-vaga.pdf"


def test_pdf_renders_llm_punctuation_and_is_the_same_every_time(resume: Resume, contact: Contact) -> None:
    result = tailored(resume).model_copy(update={"resumo": "Dados — pipelines… “rápidos” • ok"})

    first = render_pdf(contact, resume.titulo, result, resume.formacao, resume.idiomas)
    again = render_pdf(contact, resume.titulo, result, resume.formacao, resume.idiomas)

    assert first.startswith(b"%PDF")
    assert first == again


@pytest.mark.parametrize("char", ["\u2010", "\u2011", "\u2012", "\u2212", "\u2013"])
def test_pdf_text_turns_any_dash_into_a_hyphen_not_a_question_mark(char: str) -> None:
    # The PDF showed "full?stack" and "Front?end" for a hyphen the LLM wrote (seen on 2026-11-18).
    assert _latin1(f"full{char}stack\u202fe\u2009Front{char}end") == "full-stack e Front-end"


async def test_tailor_builds_the_pdf_once_and_never_sends_contact_data(resume: Resume, contact: Contact) -> None:
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][1]["content"])
        content = choice().model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = build_chat(client, model="openai/gpt-oss-120b", retry_base_delay=0)
        tailor = ResumeTailor(chat, resume, contact, today=lambda: TODAY)
        first = await tailor.tailor(job(1), DETAILS)
        again = await tailor.tailor(job(1), DETAILS)

    assert first is not None
    assert first == again == tailor.cached(job(1))
    assert len(prompts) == 1
    assert first.attachment.filename == "Curriculo-Acme.pdf"
    assert first.attachment.content.startswith(b"%PDF")
    assert "Buscamos pessoa de dados com Go." in prompts[0]
    for private in ("Ana Exemplo", "00000-0000", "ana@example.com"):
        assert private not in prompts[0]


async def test_an_unusable_reply_gives_no_file(resume: Resume, contact: Contact) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"resumo": 1}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = build_chat(client, model="openai/gpt-oss-120b", retry_base_delay=0)
        result = await ResumeTailor(chat, resume, contact).tailor(job(1), DETAILS)

    assert result is None


async def test_a_pdf_that_fails_to_render_gives_no_file_instead_of_failing_the_cycle(
    resume: Resume, contact: Contact, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> bytes:
        raise RuntimeError("render bug")

    def handler(request: httpx.Request) -> httpx.Response:
        content = choice().model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("app.services.tailor.render_pdf", broken)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = build_chat(client, model="openai/gpt-oss-120b", retry_base_delay=0)
        result = await ResumeTailor(chat, resume, contact).tailor(job(1), DETAILS)

    assert result is None
