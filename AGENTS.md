# AGENTS.md — `linkedin-api`

Guide for AI agents (and humans) working in this repository. **These rules take precedence over any
default behavior.** Goal: small changes in the style of the existing code, without overengineering.

A single-process asyncio **worker**: it polls LinkedIn's public job search, keeps the new Easy Apply jobs and
alerts by email (Resend) or WhatsApp (Evolution API). No HTTP server and no database. The decisions and
their sources are in [SCOPE.md](SCOPE.md).

## Non-negotiable principles

1. **No `Any`.** `mypy` runs in `strict` mode. If a type is hard, model it.
2. **Everything async.** Network calls go through the shared `httpx.AsyncClient`; file writes go through
   `asyncio.to_thread`. No `requests`, no `time.sleep`.
3. **Single event loop, no locks.** Breakers, the seen-set and the WhatsApp hourly cap live in memory. There
   is no `await` between reading and writing that state. One replica only.
4. **Never log secrets or personal data.** No headers, bodies or URLs with keys. Mask email and phone with
   `app/utils/mask.py`; pass any provider error text through `redact()`.
5. **Keys only in headers**, never in a URL or query string.
6. **Source data is untrusted.** It crosses `schemas/job.py` before anything else. Links are rebuilt from the
   id, never copied.
7. **Never log in to LinkedIn and never send cookies.** No browser automation, no applying for jobs.
8. **WhatsApp sends only to `WHATSAPP_TO`.** Nothing reads contacts or replies to messages.
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
  sources/                 # JobSource protocol + linkedin_guest (parser of the public search page)
  channels/                # Notifier protocol + email (Resend) + whatsapp (Evolution)
  services/                # watcher (the cycle), dispatcher (primary -> fallback), messages (text),
                           # assessor (job score by an OpenAI-compatible LLM)
  state/seen_store.py      # seen job keys + baselines; memory or STATE_FILE
  utils/                   # circuit_breaker, retry, mask
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
- **New source or channel:** a class with the protocol's shape, wired in `main.py`. Nothing else changes.

## Mandatory validation

Before finishing any change, run `make check` (ruff, ruff format --check, mypy strict, pytest) and report
the real result.

## Conventions

- Comments in English, short, only where the code does not explain itself.
- User-facing message text (`services/messages.py`) is in Portuguese.
- Never read or commit `.env`. Use `.env.example` as the reference.
- Dependencies: edit `requirements.in`, then `make lock`.
