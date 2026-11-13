import re

_EMAIL = re.compile(r"([\w.+-])[\w.+-]*@([\w-]+(?:\.[\w-]+)+)")
_LONG_NUMBER = re.compile(r"\d{8,}")
# A Telegram bot token (123456:AA...): it travels in the request URL, so it must never reach a log line.
_BOT_TOKEN = re.compile(r"\d{5,}:[\w-]{20,}")


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if local and domain else "***"


def mask_phone(value: str) -> str:
    digits = value.split("@", 1)[0]
    return f"{digits[:4]}*****{digits[-4:]}" if len(digits) > 8 else "***"


def redact(text: str) -> str:
    """Masks bot tokens, emails and long digit runs (phone numbers) in free text, such as a provider's error."""
    text = _BOT_TOKEN.sub("<bot-token>", text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return _LONG_NUMBER.sub(lambda m: f"{m.group()[:4]}***", text)
