# AGENTS.md — `linkedin-api`

Guide for AI agents (and humans) working in this repository. **These rules take precedence over any
default behavior.** Goal: small changes in the style of the existing code, without overengineering.

A single-process asyncio **worker**: it polls the public job searches of LinkedIn, Gupy, Remotar, Himalayas and
Workable (all at once, every cycle), scores the new jobs
(any kind of application; it never applies) and alerts by email (Resend), WhatsApp (Evolution API) or Telegram
(Bot API). No HTTP server and no database. The decisions and their sources are in [SCOPE.md](SCOPE.md).

## Non-negotiable principles

1. **No `Any`.** `mypy` runs in `strict` mode. If a type is hard, model it.
2. **Everything async.** Network calls go through the shared `httpx.AsyncClient`; file writes go through
   `asyncio.to_thread`. No `requests`, no `time.sleep`.
3. **Single event loop, no locks.** Breakers, the seen-set and the WhatsApp hourly cap live in memory. There
   is no `await` between reading and writing that state. One replica only.
4. **Never log secrets or personal data.** No headers, bodies or URLs with keys. Mask email and phone with
   `app/utils/mask.py`; pass any provider error text through `redact()`. The resume's `contato` block never
   goes to an LLM, and test fixtures use made-up people only.
5. **Keys only in headers**, never in a URL or query string.
6. **Source data is untrusted.** It crosses `schemas/job.py` before anything else. Links are rebuilt from the
   id, never copied. The one exception is Gupy's career subdomain, taken from the page only after it is
   validated as a single DNS label under `gupy.io`.
7. **Never log in to LinkedIn and never send cookies.** No browser automation, no applying for jobs.
8. **WhatsApp sends only to `WHATSAPP_TO`, Telegram only to `TELEGRAM_CHAT_ID`.** Nothing reads contacts,
   bot updates or replies to messages. The Telegram token sits in the request URL: never log a URL or `str()`
   of an httpx error.
9. **No dead code, no duplication.** Look for an existing helper before writing a new one.
10. **No overengineering.** Implement what was asked.

## Layout

```
app/
  main.py                  # boot, signal handling, wiring
  config/                  # settings (env validation), logging
  errors.py                # SourceUnavailableError, RetryableError, Channel*Error
  schemas/job.py           # Job, JobDetails, Assessment: the boundary for source and LLM data
  schemas/resume.py        # resume YAML without the contact block (the only part an LLM sees)
  sources/                 # JobSource protocol + DetailsCache + linkedin_guest (public search page) +
                           # gupy_portal (__NEXT_DATA__ JSON of portal.gupy.io) + remotar, himalayas,
                           # workable (public JSON); all read on every cycle, at the same time
  channels/                # Notifier protocol + email (Resend) + whatsapp (Evolution) + telegram (Bot API)
  services/                # watcher (the cycle), dispatcher (primary -> fallback), messages (text),
                           # llm (chat client, one breaker per model), assessor (job score),
                           # tailor (resume for jobs scored 50+), resume_pdf (fpdf2 render),
                           # gupy_text (the same resume as text for Gupy's typed fields)
  state/seen_store.py      # seen job keys + baselines; memory or STATE_FILE
  utils/                   # circuit_breaker, retry, mask, watchdog (exit 1 on a stalled loop),
                           # search_query (NOT terms and OR alternatives of a search), html_text
tests/
```

**Rules per layer:**
- **A source** returns `list[Job]` or raises `SourceUnavailableError`. It owns its own breaker.
- **A channel** returns when the provider accepted the message. Otherwise it raises
  `ChannelUnavailableError` (transient) or `ChannelRejectedError` (4xx). It owns its own breaker and
  retries.
- **The dispatcher** only orders the channels.
- **The watcher** only decides what is new and what is marked as seen. A job is marked **only after**
  a channel accepted it.
- **New source:** a class with the `JobSource` shape (`name`, `pages_need_requests`, `fetch_recent`,
  `fetch_details`), added to the `sources` list in `main.py`; its name and id shape go in `Source`,
  `SOURCE_LABELS` and `_ID_SHAPES` (`schemas/job.py`). A search that already brings the description (no OR/NOT)
  subclasses `TermSearchSource` (`sources/base.py`) and writes only `_fetch_term` and `_read`.
- **New channel:** a class with the `Notifier` shape, wired in `main.py`. Nothing else changes.

## Mandatory validation

Before finishing any change, run `make check` (ruff, ruff format --check, mypy strict, pytest) and report
the real result.

## Conventions

- Comments in English, short, only where the code does not explain itself.
- User-facing message text (`services/messages.py`) is in Portuguese.
- Never read or commit `.env`. Use `.env.example` as the reference.
- Dependencies: edit `requirements.in`, then `make lock`.
