import asyncio
import random
from collections.abc import Awaitable, Callable

from app.errors import RetryableError

MAX_DELAY_SECONDS = 30.0


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header. Only the delta-seconds form; an HTTP date is ignored."""
    if value and value.strip().isdigit():
        return float(value.strip())
    return None


def backoff(attempt: int, base: float, retry_after: float | None = None) -> float:
    """Delay before retry `attempt` (1-based): exponential with jitter, Retry-After wins when longer."""
    ceiling = min(MAX_DELAY_SECONDS, base * 2.0 ** (attempt - 1))
    delay = ceiling / 2 + random.uniform(0, ceiling / 2)  # noqa: S311 - jitter, not security
    if retry_after is not None:
        delay = max(delay, min(retry_after, MAX_DELAY_SECONDS))
    return delay


async def retrying[T](call: Callable[[], Awaitable[T]], *, attempts: int, base_delay: float) -> T:
    """Runs `call` up to `attempts` times while it raises RetryableError. The last one propagates."""
    attempt = 1
    while True:
        try:
            return await call()
        except RetryableError as e:
            if attempt >= attempts:
                raise
            await asyncio.sleep(backoff(attempt, base_delay, e.retry_after))
            attempt += 1
