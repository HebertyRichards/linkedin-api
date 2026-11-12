import logging
from collections.abc import Mapping

import httpx

from app.config.settings import Settings
from app.errors import RetryableError
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.mask import redact
from app.utils.retry import parse_retry_after, retrying

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 60.0
_ATTEMPTS = 4
# Groq's token-per-minute window answers 429 with a Retry-After of up to ~60 s (observed: 1 to 59 s with an
# 8,000 tokens/min limit, i.e. 2-3 jobs a minute). That is waited out; only a longer one is the daily quota.
_MINUTE_WINDOW_SECONDS = 60.0


class ChatClient:
    """One model behind an OpenAI-compatible chat API (Groq by default), with its own breaker and retries.

    Groq limits each model separately, so each model gets its own client. Never raises: any failure is None.
    """

    def __init__(
        self, client: httpx.AsyncClient, settings: Settings, *, model: str, retry_base_delay: float = 1.0
    ) -> None:
        self._client = client
        self._endpoint = f"{settings.llm_api_url}/chat/completions"
        self._api_key = settings.llm_api_key
        self.model = model
        self._retry_base_delay = retry_base_delay
        self._breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=60.0, max_recovery_timeout=3600.0, name=f"llm {model}"
        )

    async def complete(self, system: str, user: str, response_format: Mapping[str, object]) -> str | None:
        """The reply text (JSON under `response_format`), or None when the model could not answer."""
        if not self._breaker.allow():
            return None
        try:
            response = await retrying(
                lambda: self._post(system, user, response_format),
                attempts=_ATTEMPTS,
                base_delay=self._retry_base_delay,
                max_delay=_MINUTE_WINDOW_SECONDS,
            )
        except RetryableError as e:
            self._breaker.record_failure()
            logger.warning("LLM %s unavailable: %s", self.model, e)
            return None
        if response.status_code == 429:
            # Quota used up for longer than a retry can wait: stop asking until the window passes.
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            self._breaker.trip(retry_after)
            logger.warning("LLM %s quota used up (retry after %ss)", self.model, retry_after)
            return None
        if response.is_error:
            # 4xx: wrong key, model or request. Configuration, so it would fail for every job.
            self._breaker.record_failure()
            logger.error("LLM %s refused: status %d: %s", self.model, response.status_code, redact(response.text[:300]))
            return None
        self._breaker.record_success()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            logger.warning("LLM %s reply unusable: %s", self.model, type(e).__name__)
            return None
        return content if isinstance(content, str) else None

    async def _post(self, system: str, user: str, response_format: Mapping[str, object]) -> httpx.Response:
        body: dict[str, object] = {
            "model": self.model,
            "temperature": 0,
            "response_format": response_format,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if "gpt-oss" in self.model:
            # Groq accepts this only on gpt-oss. Low effort keeps reasoning tokens off the free quota.
            body["reasoning_effort"] = "low"
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                json=body,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise RetryableError(f"network error: {type(e).__name__}") from e
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            if response.status_code == 429 and retry_after is not None and retry_after > _MINUTE_WINDOW_SECONDS:
                return response  # a quota window, not a blip: complete() opens the breaker instead
            raise RetryableError(f"status {response.status_code}", retry_after)
        return response
