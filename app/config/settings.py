import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Channel = Literal["email", "whatsapp"]

MAX_SEARCHES = 3
# A number with country and area code (5511999999999), or a WhatsApp JID (contact or group).
_WHATSAPP_TO = re.compile(r"^\d{10,15}$|^\d{10,15}@s\.whatsapp\.net$|^[\d-]+@g\.us$")
# Plain http only where the traffic never leaves the machine or Railway's private network.
_PLAIN_HTTP_HOSTS = {"localhost", "127.0.0.1"}
_PRIVATE_HOST_SUFFIX = ".railway.internal"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=False)

    job_keywords: str = Field(...)
    job_location: str = Field(..., min_length=2)
    poll_interval_seconds: int = Field(default=120, ge=60)
    state_file: str = Field(default="")

    resend_api_key: SecretStr = Field(default=SecretStr(""))
    email_to: str = Field(default="")
    email_from: str = Field(default="onboarding@resend.dev")

    evolution_api_url: str = Field(default="")
    evolution_api_key: SecretStr = Field(default=SecretStr(""))
    evolution_instance: str = Field(default="")
    whatsapp_to: str = Field(default="")

    notify_primary: Channel = Field(default="whatsapp")

    @field_validator("resend_api_key", "evolution_api_key", mode="before")
    @classmethod
    def _strip_secret(cls, v: object) -> object:
        # Values pasted from a dashboard often carry a stray space or newline.
        return "".join(v.split()) if isinstance(v, str) else v

    @field_validator("job_keywords")
    @classmethod
    def _keywords(cls, v: str) -> str:
        searches = [part.strip() for part in v.split(",") if part.strip()]
        if not 1 <= len(searches) <= MAX_SEARCHES:
            raise ValueError(f"must hold 1 to {MAX_SEARCHES} searches separated by commas")
        return ",".join(searches)

    @field_validator("email_to", "email_from")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if v and ("@" not in v or " " in v):
            raise ValueError("must be an email address")
        return v

    @field_validator("evolution_api_url")
    @classmethod
    def _evolution_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            return v
        parts = urlsplit(v)
        host = parts.hostname or ""
        private = host in _PLAIN_HTTP_HOSTS or host.endswith(_PRIVATE_HOST_SUFFIX)
        if parts.scheme != "https" and not (parts.scheme == "http" and private):
            raise ValueError("must start with https:// (http only for localhost or *.railway.internal)")
        return v

    @field_validator("whatsapp_to")
    @classmethod
    def _whatsapp_to(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v:
            # A number pasted as "+55 (11) 99999-9999" keeps only its digits.
            v = "".join(ch for ch in v if ch.isdigit())
        if v and not _WHATSAPP_TO.match(v):
            raise ValueError("must be a number with country and area code (5511999999999) or a group id (...@g.us)")
        return v

    @model_validator(mode="after")
    def _channels_complete(self) -> Self:
        required = {
            "email": {"RESEND_API_KEY": self.resend_api_key.get_secret_value(), "EMAIL_TO": self.email_to},
            "whatsapp": {
                "EVOLUTION_API_URL": self.evolution_api_url,
                "EVOLUTION_API_KEY": self.evolution_api_key.get_secret_value(),
                "EVOLUTION_INSTANCE": self.evolution_instance,
                "WHATSAPP_TO": self.whatsapp_to,
            },
        }
        for channel, fields in required.items():
            missing = [name for name, value in fields.items() if not value]
            if missing and len(missing) < len(fields):
                raise ValueError(f"the {channel} channel is incomplete, missing: {', '.join(missing)}")
        if not self.channels:
            raise ValueError(
                "configure at least one channel: RESEND_API_KEY + EMAIL_TO, or "
                "EVOLUTION_API_URL + EVOLUTION_API_KEY + EVOLUTION_INSTANCE + WHATSAPP_TO"
            )
        return self

    @property
    def searches(self) -> list[str]:
        return self.job_keywords.split(",")

    @property
    def channels(self) -> list[Channel]:
        """Configured channels, the primary first."""
        configured: list[Channel] = []
        if self.resend_api_key.get_secret_value() and self.email_to:
            configured.append("email")
        if self.evolution_api_url and self.evolution_api_key.get_secret_value() and self.whatsapp_to:
            configured.append("whatsapp")
        return sorted(configured, key=lambda name: name != self.notify_primary)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        # Raised without the original traceback, which would echo values read from the environment.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" if err["loc"] else err["msg"]
            for err in e.errors()
        )
        raise RuntimeError(f"Invalid or missing configuration: {problems}. See .env.example.") from None
