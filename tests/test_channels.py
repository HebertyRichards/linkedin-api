import base64
import json
import logging
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from app.channels.email import EmailNotifier
from app.channels.telegram import MAX_TEXT, TelegramNotifier, chunks
from app.channels.whatsapp import WhatsAppNotifier
from app.config.logging_config import setup_logging
from app.errors import ChannelRejectedError, ChannelUnavailableError
from app.main import build_channels
from app.services.messages import job_message
from app.services.tailor import TailoredFile
from tests.helpers import DETAILS, TELEGRAM, TELEGRAM_TOKEN, WHATSAPP, FakeTailor, job, make_settings

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


def telegram(on_call: Callable[[str, httpx.Request], httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{TELEGRAM_TOKEN}/"
        assert request.url.host == "api.telegram.org"
        assert request.url.path.startswith(prefix)
        return on_call(request.url.path.removeprefix(prefix), request)

    return handler


def ok(method: str, request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def telegram_notifier(client: httpx.AsyncClient) -> TelegramNotifier:
    return TelegramNotifier(client, make_settings(**TELEGRAM), retry_base_delay=0, file_spacing=0)


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


async def test_telegram_sends_html_to_the_chat_and_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    # Only the app's loggers: httpx logs each URL at INFO, and setup_logging keeps it at WARNING.
    caplog.set_level(logging.DEBUG, logger="app")
    calls: list[tuple[str, httpx.Request]] = []

    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        calls.append((method, request))
        return ok(method, request)

    async with client_for(telegram(on_call)) as client:
        await telegram_notifier(client).send(MESSAGE)

    assert [method for method, _ in calls] == ["sendMessage"]  # no file without a tailored resume
    body = form(calls[0][1])
    assert body["chat_id"] == "987654321"
    assert body["parse_mode"] == "HTML"
    assert f"<b>{job(1).title}</b>" in body["text"]
    assert f'<a href="{job(1).url}">' in body["text"]
    assert TELEGRAM_TOKEN not in caplog.text


def test_telegram_html_escapes_what_comes_from_outside() -> None:
    risky = job(1, title="C++ <Dev> & 'Lead'")
    blocks = job_message([risky]).telegram_blocks

    assert "<b>C++ &lt;Dev&gt; &amp; 'Lead'" in blocks[1]
    assert "&#x27;" not in "".join(blocks)  # Telegram answers 400 to the entity escape() writes for '


async def test_telegram_sends_the_resume_after_the_text_with_the_job_in_the_caption() -> None:
    calls: list[tuple[str, httpx.Request]] = []

    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        calls.append((method, request))
        return ok(method, request)

    resume = await FakeTailor().tailor(job(1), DETAILS)
    assert isinstance(resume, TailoredFile)
    async with client_for(telegram(on_call)) as client:
        await telegram_notifier(client).send(job_message([job(1)], tailored={job(1).key: resume}))

    assert [method for method, _ in calls] == ["sendMessage", "sendDocument"]
    upload = calls[1][1].content
    assert b'filename="Curriculo-4400000001.pdf"' in upload
    assert b"%PDF-fake" in upload
    assert "Currículo para: Python Developer".encode() in upload


async def test_telegram_long_digest_goes_in_several_messages_without_cutting_a_job() -> None:
    texts: list[str] = []

    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        texts.append(form(request)["text"])
        return ok(method, request)

    jobs = [job(n, title="Desenvolvedor Python " + "x" * 190) for n in range(1, 31)]
    async with client_for(telegram(on_call)) as client:
        await telegram_notifier(client).send(job_message(jobs))

    assert len(texts) > 1
    assert all(len(text) <= MAX_TEXT for text in texts)
    assert "".join(texts).count("Ver vaga e se candidatar") == 30


def test_chunks_never_split_a_block() -> None:
    assert chunks(["a" * 6, "b" * 6, "c" * 6], limit=14) == ["a" * 6 + "\n\n" + "b" * 6, "c" * 6]


async def test_telegram_failed_resume_fails_the_message_so_the_reserve_sends_it_whole() -> None:
    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        if method == "sendDocument":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: file is empty"})
        return ok(method, request)

    resume = await FakeTailor().tailor(job(1), DETAILS)
    assert isinstance(resume, TailoredFile)
    async with client_for(telegram(on_call)) as client:
        with pytest.raises(ChannelRejectedError, match="sendDocument: status 400: Bad Request"):
            await telegram_notifier(client).send(job_message([job(1)], tailored={job(1).key: resume}))


async def test_telegram_short_rate_limit_waits_and_retries() -> None:
    attempts = 0

    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return ok(method, request)

    async with client_for(telegram(on_call)) as client:
        await telegram_notifier(client).send(MESSAGE)

    assert attempts == 2


async def test_telegram_long_rate_limit_hands_over_to_the_next_channel() -> None:
    attempts = 0

    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 600}})

    async with client_for(telegram(on_call)) as client:
        with pytest.raises(ChannelUnavailableError, match="rate limited"):
            await telegram_notifier(client).send(MESSAGE)

    assert attempts == 1


async def test_telegram_blocked_bot_is_rejected_with_the_token_masked() -> None:
    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": f"Forbidden: bot {TELEGRAM_TOKEN} was blocked"})

    async with client_for(telegram(on_call)) as client:
        with pytest.raises(ChannelRejectedError, match="Forbidden") as error:
            await telegram_notifier(client).send(MESSAGE)

    assert TELEGRAM_TOKEN not in str(error.value)
    assert "<bot-token>" in str(error.value)


async def test_telegram_read_timeout_is_not_retried_and_hides_the_url() -> None:
    attempts = 0

    def on_call(method: str, request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout(f"timed out on {request.url}", request=request)

    async with client_for(telegram(on_call)) as client:
        with pytest.raises(ChannelUnavailableError) as error:
            await telegram_notifier(client).send(MESSAGE)

    assert attempts == 1
    assert TELEGRAM_TOKEN not in str(error.value)


def test_request_urls_stay_out_of_the_logs_even_at_debug() -> None:
    root = logging.getLogger()
    saved = root.level, root.handlers[:], {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        setup_logging("DEBUG", json_logs=True)
        # The Telegram token travels in the URL, and httpx logs each URL at INFO.
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
        for name, level in saved[2].items():
            logging.getLogger(name).setLevel(level)


async def test_every_configured_channel_gets_its_notifier_primary_first() -> None:
    settings = make_settings(**WHATSAPP, **TELEGRAM, notify_primary="telegram")
    async with httpx.AsyncClient() as client:
        channels = build_channels(settings, client)

    assert [channel.name for channel in channels] == ["telegram", "email", "whatsapp"]
    assert isinstance(channels[0], TelegramNotifier)
