import json
import logging

import httpx
import pytest

from app.services.llm import ChatClient, FallbackChat, build_chats
from tests.helpers import FALLBACK, build_chat, make_settings

SCHEMA: dict[str, object] = {"type": "json_object"}
LLM = {"llm_api_key": "gsk_test_key", "resume_file": "curriculo.yaml"}


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def quota_used_up() -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": "rate limit"})


def pair(handler: httpx.MockTransport) -> tuple[FallbackChat, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    primary = build_chat(client, model="openai/gpt-oss-20b")
    fallback = build_chat(client, model="openai/gpt-oss-20b:free", url="https://openrouter.ai/api/v1")
    return FallbackChat(primary, fallback), client


async def test_the_fallback_answers_when_the_primary_quota_is_used_up(caplog: pytest.LogCaptureFixture) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return quota_used_up() if request.url.host == "api.groq.com" else reply('{"ok": true}')

    chat, client = pair(httpx.MockTransport(handler))
    caplog.set_level(logging.INFO, logger="app")
    async with client:
        assert await chat.complete("system", "user", SCHEMA) == '{"ok": true}'
        # The primary breaker is open now: the next job goes straight to the fallback.
        assert await chat.complete("system", "user", SCHEMA) == '{"ok": true}'

    assert hosts == ["api.groq.com", "openrouter.ai", "openrouter.ai"]
    assert "answered instead" in caplog.text


async def test_the_fallback_is_not_asked_when_the_primary_answers() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return reply('{"ok": true}')

    chat, client = pair(httpx.MockTransport(handler))
    async with client:
        assert await chat.complete("system", "user", SCHEMA) == '{"ok": true}'
    assert hosts == ["api.groq.com"]


async def test_both_providers_down_is_no_answer() -> None:
    chat, client = pair(httpx.MockTransport(lambda _: quota_used_up()))
    async with client:
        assert await chat.complete("system", "user", SCHEMA) is None


async def test_reasoning_effort_goes_only_to_groq() -> None:
    bodies: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies[request.url.host] = json.loads(request.content)
        return reply("{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await build_chat(client, model="openai/gpt-oss-20b").complete("s", "u", SCHEMA)
        other = build_chat(client, model="openai/gpt-oss-20b:free", url="https://openrouter.ai/api/v1")
        await other.complete("s", "u", SCHEMA)

    assert bodies["api.groq.com"]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in bodies["openrouter.ai"]


async def test_build_chats_backs_both_models_with_one_fallback_client() -> None:
    async with httpx.AsyncClient() as client:
        score, tailor = build_chats(client, make_settings(**LLM))
        assert isinstance(score, ChatClient) and isinstance(tailor, ChatClient)
        assert (score.model, tailor.model) == ("openai/gpt-oss-20b", "openai/gpt-oss-120b")

        score, tailor = build_chats(client, make_settings(**LLM, **FALLBACK))
        assert isinstance(score, FallbackChat) and isinstance(tailor, FallbackChat)
        assert score._fallback is tailor._fallback
