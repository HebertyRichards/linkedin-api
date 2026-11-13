import pytest
from pydantic import ValidationError

from app.config.settings import get_settings
from tests.helpers import TELEGRAM, TELEGRAM_TOKEN, WHATSAPP, make_settings


def test_email_alone_is_enough() -> None:
    settings = make_settings()
    assert settings.channels == ["email"]
    assert settings.email_from == "onboarding@resend.dev"


def test_primary_channel_comes_first() -> None:
    assert make_settings(**WHATSAPP).channels == ["whatsapp", "email"]
    assert make_settings(**WHATSAPP, notify_primary="email").channels == ["email", "whatsapp"]


def test_primary_without_its_channel_uses_the_configured_one() -> None:
    assert make_settings(notify_primary="whatsapp").channels == ["email"]


def test_incomplete_channel_names_what_is_missing() -> None:
    partial = {key: value for key, value in WHATSAPP.items() if key != "evolution_instance"}
    with pytest.raises(ValidationError, match="whatsapp channel is incomplete, missing: EVOLUTION_INSTANCE"):
        make_settings(**partial)


def test_telegram_alone_is_enough() -> None:
    assert make_settings(**TELEGRAM, resend_api_key="", email_to="").channels == ["telegram"]


def test_all_three_channels_keep_their_order_after_the_primary() -> None:
    assert make_settings(**WHATSAPP, **TELEGRAM).channels == ["whatsapp", "email", "telegram"]
    assert make_settings(**WHATSAPP, **TELEGRAM, notify_primary="telegram").channels == [
        "telegram",
        "email",
        "whatsapp",
    ]


def test_incomplete_telegram_names_what_is_missing() -> None:
    with pytest.raises(ValidationError, match="telegram channel is incomplete, missing: TELEGRAM_CHAT_ID"):
        make_settings(telegram_bot_token=TELEGRAM_TOKEN)


@pytest.mark.parametrize("chat_id", ["@meu_canal", "https://t.me/x", "12 34"])
def test_telegram_chat_id_must_be_numeric(chat_id: str) -> None:
    with pytest.raises(ValidationError, match="numeric chat id"):
        make_settings(**{**TELEGRAM, "telegram_chat_id": chat_id})
    assert make_settings(**{**TELEGRAM, "telegram_chat_id": " -1001234567890 "}).telegram_chat_id == "-1001234567890"


def test_malformed_telegram_token_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValidationError, match="@BotFather") as error:
        make_settings(**{**TELEGRAM, "telegram_bot_token": "not-a-token-value"})
    assert "not-a-token-value" not in str(error.value)


def test_no_channel_is_an_error() -> None:
    with pytest.raises(ValidationError, match="configure at least one channel"):
        make_settings(resend_api_key="", email_to="")


@pytest.mark.parametrize(
    "url",
    ["https://evolution.example.com/", "http://evolution.railway.internal:8080", "http://localhost:8080"],
)
def test_evolution_url_accepted(url: str) -> None:
    assert make_settings(**{**WHATSAPP, "evolution_api_url": url}).evolution_api_url == url.rstrip("/")


def test_evolution_url_without_tls_is_rejected() -> None:
    with pytest.raises(ValidationError, match="https://"):
        make_settings(**{**WHATSAPP, "evolution_api_url": "http://evolution.example.com"})


def test_whatsapp_number_pasted_with_formatting() -> None:
    settings = make_settings(**{**WHATSAPP, "whatsapp_to": "+55 (11) 99999-9999"})
    assert settings.whatsapp_to == "5511999999999"
    assert make_settings(**{**WHATSAPP, "whatsapp_to": "120363000000000000@g.us"}).whatsapp_to.endswith("@g.us")


def test_searches_are_trimmed_and_capped() -> None:
    assert make_settings(job_keywords=" python , ,typescript ").searches == ["python", "typescript"]
    with pytest.raises(ValidationError, match="1 to 3 searches"):
        make_settings(job_keywords="a,b,c,d")


def test_poll_interval_has_a_floor() -> None:
    with pytest.raises(ValidationError):
        make_settings(poll_interval_seconds=59)


def test_startup_error_lists_names_but_never_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_KEYWORDS", "python")
    monkeypatch.setenv("JOB_LOCATION", "Brazil")
    monkeypatch.setenv("RESEND_API_KEY", "re_super_secret_123")
    monkeypatch.setenv("EMAIL_TO", "not an email")
    with pytest.raises(RuntimeError) as error:
        get_settings()
    message = str(error.value)
    assert "EMAIL_TO" in message
    assert "re_super_secret_123" not in message
    assert "not an email" not in message


def test_assessment_needs_key_and_resume_together() -> None:
    assert not make_settings().assessment_enabled
    assert make_settings(llm_api_key="gsk_x", resume_file="curriculo.yaml").assessment_enabled
    assert make_settings(llm_api_key="gsk_x", resume_yaml="titulo: Dev").assessment_enabled
    with pytest.raises(ValidationError, match="needs LLM_API_KEY and a resume"):
        make_settings(llm_api_key="gsk_x")
    with pytest.raises(ValidationError, match="RESUME_FILE or RESUME_YAML, not both"):
        make_settings(llm_api_key="gsk_x", resume_file="curriculo.yaml", resume_yaml="titulo: Dev")


def test_resume_yaml_never_shows_in_errors() -> None:
    with pytest.raises(ValidationError) as error:
        make_settings(resume_yaml="contato: {telefone: '(00) 12345-6789'}")
    assert "12345" not in str(error.value)


def test_llm_url_must_be_https_except_on_this_machine() -> None:
    assert make_settings(llm_api_url="http://localhost:11434/v1").llm_api_url == "http://localhost:11434/v1"
    with pytest.raises(ValidationError, match="must start with https"):
        make_settings(llm_api_url="http://api.example.com/v1")
