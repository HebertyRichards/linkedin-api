import base64
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from app.channels.email import EmailNotifier
from app.channels.whatsapp import WhatsAppNotifier
from app.errors import ChannelRejectedError, ChannelUnavailableError
from app.services.messages import job_message
from app.services.tailor import TailoredFile
from tests.helpers import DETAILS, WHATSAPP, FakeTailor, job, make_settings

Handler = Callable[[httpx.Request], httpx.Response]
MESSAGE = job_message([job(1)])


def client_for(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_email_sends_the_key_only_in_the_header_and_never_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "email-1"})

    async with client_for(handler) as client:
        await EmailNotifier(client, make_settings(), retry_base_delay=0).send(MESSAGE)

    request = requests[0]
    assert request.headers["authorization"] == "Bearer re_test_key"
    assert request.headers["idempotency-key"] == MESSAGE.idempotency_key
    assert json.loads(request.content)["to"] == ["me@example.com"]
    assert "re_test_key" not in str(request.url)
    assert "re_test_key" not in caplog.text
    assert "me@example.com" not in caplog.text


async def test_email_attaches_the_tailored_resume_only_when_there_is_one() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "email-1"})

    resume = await FakeTailor().tailor(job(1), DETAILS)
    assert isinstance(resume, TailoredFile)
    with_file = job_message([job(1)], tailored={job(1).key: resume})
    async with client_for(handler) as client:
        notifier = EmailNotifier(client, make_settings(), retry_base_delay=0)
        await notifier.send(with_file)
        await notifier.send(MESSAGE)

    assert bodies[0]["attachments"] == [
        {"filename": "Curriculo-4400000001.pdf", "content": base64.b64encode(b"%PDF-fake").decode()}
    ]
    assert "attachments" not in bodies[1]
    assert with_file.idempotency_key != MESSAGE.idempotency_key


async def test_email_retries_a_server_error_with_the_same_idempotency_key() -> None:
    answers = iter([500, 200])
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(next(answers), json={})

    async with client_for(handler) as client:
        await EmailNotifier(client, make_settings(), retry_base_delay=0).send(MESSAGE)

    assert keys == [MESSAGE.idempotency_key, MESSAGE.idempotency_key]


async def test_email_refusal_is_rejected_with_the_address_masked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "You can only send testing emails to me@example.com"})

    async with client_for(handler) as client:
        with pytest.raises(ChannelRejectedError) as error:
            await EmailNotifier(client, make_settings(), retry_base_delay=0).send(MESSAGE)

    assert "me@example.com" not in str(error.value)
    assert "m***@example.com" in str(error.value)


async def test_email_network_failure_is_unavailable_after_the_retries() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("down", request=request)

    async with client_for(handler) as client:
        with pytest.raises(ChannelUnavailableError):
            await EmailNotifier(client, make_settings(), retry_base_delay=0).send(MESSAGE)

    assert attempts == 3


def evolution(state: str, on_send: Callable[[httpx.Request], httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "evo_test_key"
        if request.url.path == "/instance/connectionState/job-alerts":
            return httpx.Response(200, json={"instance": {"instanceName": "job-alerts", "state": state}})
        return on_send(request)

    return handler


async def test_whatsapp_sends_only_to_the_configured_number() -> None:
    sent: list[httpx.Request] = []

    def on_send(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"key": {"id": "wa-1"}})

    async with client_for(evolution("open", on_send)) as client:
        await WhatsAppNotifier(client, make_settings(**WHATSAPP), retry_base_delay=0).send(MESSAGE)

    assert sent[0].url.path == "/message/sendText/job-alerts"
    assert json.loads(sent[0].content)["number"] == "5511999999999"


async def test_whatsapp_disconnected_never_tries_to_send() -> None:
    def on_send(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send while the instance is disconnected")

    async with client_for(evolution("close", on_send)) as client:
        with pytest.raises(ChannelUnavailableError, match="not connected"):
            await WhatsAppNotifier(client, make_settings(**WHATSAPP), retry_base_delay=0).send(MESSAGE)


async def test_whatsapp_read_timeout_is_not_retried() -> None:
    attempts = 0

    def on_send(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("no answer", request=request)

    async with client_for(evolution("open", on_send)) as client:
        with pytest.raises(ChannelUnavailableError, match="no answer"):
            await WhatsAppNotifier(client, make_settings(**WHATSAPP), retry_base_delay=0).send(MESSAGE)

    assert attempts == 1


async def test_whatsapp_connect_error_is_retried() -> None:
    attempts = 0

    def on_send(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(201, json={})

    async with client_for(evolution("open", on_send)) as client:
        await WhatsAppNotifier(client, make_settings(**WHATSAPP), retry_base_delay=0).send(MESSAGE)

    assert attempts == 2


async def test_whatsapp_hourly_cap_hands_over_to_the_next_channel() -> None:
    async with client_for(evolution("open", lambda request: httpx.Response(201, json={}))) as client:
        notifier = WhatsAppNotifier(client, make_settings(**WHATSAPP), retry_base_delay=0, hourly_cap=1)
        await notifier.send(MESSAGE)
        with pytest.raises(ChannelUnavailableError, match="hourly cap"):
            await notifier.send(MESSAGE)
