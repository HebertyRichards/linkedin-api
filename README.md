# linkedin-api

A small worker that watches LinkedIn job searches and alerts you, by **email (Resend)** or
**WhatsApp (Evolution API)**, as soon as a new **Easy Apply** job shows up: the kind you apply to
inside LinkedIn itself. One of the two channels must accept each alert; the other is the fallback.

It does not apply for you, does not list every job, and needs no database.

> **Read this first.** LinkedIn has no official API for this (see [SCOPE.md](SCOPE.md), section 1).
> The worker reads LinkedIn's public, logged-out job search. LinkedIn's User Agreement (§8.2) forbids
> automated access, so the risk of blocking is yours. The worker keeps it low on purpose: one request per
> search per cycle, no login, no cookies, and it backs off when LinkedIn answers 429. Never point it at a
> logged-in session.

## How it works

```
at boot, then every 4 hours: digest
  for each search: Easy Apply jobs posted in the last 3 days, never older (f_TPR=r259200)
  send up to 10 not sent yet in one message (catches jobs that reached the search late)

every ~30 min (±20%)
  for each search (max 3): Easy Apply jobs posted in the last hour (f_AL=true, f_TPR=r3600), all pages
  new = job ids not seen before
  first read of a search = baseline: what is already posted is marked as seen, nothing is sent
  send: primary channel -> fallback channel
  accepted -> remember the ids    both failed -> keep them pending, the next cycle tries again
```

- **One alert per job.** When more than 3 new jobs arrive in one cycle, they go together, up to 10 per
  message. Batching saves the free Resend quota (100 emails a day) and keeps WhatsApp from looking like
  bulk sending.
- **Links are rebuilt from the job id** (`https://www.linkedin.com/jobs/view/<id>/`), never copied from the
  page.
- **"Delivered" means the provider accepted the message** (a 2xx from Resend or Evolution). If the primary
  times out after sending and the fallback also succeeds, you get the alert twice. That is the
  at-least-once trade-off.
- **With `LLM_API_KEY` and `RESUME_FILE`, each new job gets a score (0-100)** against your resume, with the
  reason and what is missing, and the best scores go first. The AI (Groq's free tier by default) reads the
  job page and the resume without the contact block. On-site jobs are dropped. When the AI is down or the free
  quota is used up, the job still goes out, without a score.
- **If the job source fails for 1 hour in a row,** you get one "source failing" alert. It is not
  repeated until the source recovers. A failure is an error, a block, or a page the parser no longer
  recognizes (LinkedIn changed the markup or answered with a challenge page).
- **A cycle logs one line** (`Cycle done: 2 of 2 search(es) answered, 1 new job(s)`) only when it found new jobs
  or a search failed; a quiet cycle logs at `DEBUG`. The `Digest done` line every 4 hours shows the worker is
  alive. A block (429) stops the cycle at the first search, with one warning. Logs go to stdout.

## Environment variables

You need **one complete channel**. Both are recommended, because without a second channel there is no
fallback, only a retry on the next cycle. A half-configured channel stops the worker at startup. The
error names the missing variables and never prints their values.

| Variable | Required | Default | What it is |
|---|---|---|---|
| `JOB_KEYWORDS` | yes | — | Up to 3 searches, comma separated: `python, typescript`. Try each term in LinkedIn's search first: a term LinkedIn does not recognize is dropped, and the search returns jobs from any field |
| `JOB_LOCATION` | yes | — | **English** name as LinkedIn spells it: `Brazil`, `São Paulo`. `Brasil` is ignored by LinkedIn and returns US jobs |
| `RESEND_API_KEY` | email | — | Resend API key |
| `EMAIL_TO` | email | — | Who gets the email |
| `EMAIL_FROM` | no | `onboarding@resend.dev` | Sender |
| `EVOLUTION_API_URL` | WhatsApp | — | Evolution base URL, `https://` (plain `http` only for `localhost` or `*.railway.internal`) |
| `EVOLUTION_API_KEY` | WhatsApp | — | The **instance** token |
| `EVOLUTION_INSTANCE` | WhatsApp | — | Instance name |
| `WHATSAPP_TO` | WhatsApp | — | Your number with country and area code (`5511999999999`) or a group id (`...@g.us`) |
| `NOTIFY_PRIMARY` | no | `whatsapp` | Channel tried first when both are set |
| `POLL_INTERVAL_SECONDS` | no | `1800` | Seconds between cycles, minimum 60 |
| `LLM_API_KEY` | no | — | Turns on job scoring. Free key at console.groq.com |
| `RESUME_FILE` | with `LLM_API_KEY` (or `RESUME_YAML`) | — | Path of the resume YAML (git-ignored). Local runs |
| `RESUME_YAML` | with `LLM_API_KEY` (or `RESUME_FILE`) | — | The resume YAML itself, pasted as a multi-line variable. Railway |
| `LLM_API_URL` | no | `https://api.groq.com/openai/v1` | Any OpenAI-compatible API |
| `LLM_MODEL` | no | `openai/gpt-oss-20b` | Model used for the score |
| `STATE_FILE` | no | memory only | Path of the seen-jobs file, e.g. `/data/seen.json` on a volume |
| `LOG_LEVEL` | no | `INFO` | |
| `APP_ENV` | no | `development` | `production` switches the logs to JSON |
| `LOG_JSON` | no | `false` | `true` switches the logs to JSON without changing `APP_ENV` |

### How to get each value

**Resend**
1. Create an account at [resend.com](https://resend.com).
2. Go to **API Keys → Create API Key**, choose "Sending access", and set the result as `RESEND_API_KEY`.
3. Set `EMAIL_TO`. Without a verified domain, Resend only sends from `onboarding@resend.dev`, and only to
   **the email of your Resend account**, so `EMAIL_TO` must be that address.
4. To send to any address, add a domain under **Domains**, create the DNS records Resend shows, and set
   `EMAIL_FROM` to an address on that domain.

**Evolution (WhatsApp)**
1. Reuse the Evolution server you already run, and set its public URL as `EVOLUTION_API_URL`.
2. Create a **new instance** with a **dedicated number**, not a business or personal number you depend
   on. Messages that the bot starts break WhatsApp's terms, and a Baileys number can be banned.
   ```bash
   curl -X POST "$EVOLUTION_API_URL/instance/create" \
     -H "apikey: $EVOLUTION_GLOBAL_KEY" -H "Content-Type: application/json" \
     -d '{"instanceName": "job-alerts", "integration": "WHATSAPP-BAILEYS", "qrcode": true}'
   ```
3. Scan the QR code from `GET /instance/connect/job-alerts` with the dedicated phone.
4. Set the instance token as `EVOLUTION_API_KEY`, not the global key. The token is shown for the instance
   in the Evolution Manager.
5. From your own phone, send the bot a message once, so the chat exists on both sides.
6. To alert a group instead, add the bot number to the group and read the group id with
   `GET /group/fetchAllGroups/job-alerts?getParticipants=false`.

The worker sends only to `WHATSAPP_TO`. It never reads contacts and never answers messages.

## Running locally

```bash
make install          # venv + dependencies
cp .env.example .env  # fill in the values
make run
```

At boot the digest sends up to 10 jobs from the last 3 days; the first cycle then takes the baseline, and
alerts start with the next job that gets posted. Stop with Ctrl+C.

## Deploying on Railway

1. **New → GitHub Repo**, pick this repo. Railway builds the `Dockerfile`. No port and no healthcheck are
   needed: it is a worker.
2. In **Variables**, set the variables above and `APP_ENV=production`.
3. **Recommended:** keep the seen-jobs list across deploys. Without it, every deploy starts a new
   baseline, and jobs posted during the deploy are never alerted.
   - Right-click the service → **Attach Volume**, mount path `/data`.
   - Set `STATE_FILE=/data/seen.json`.
   - Set `RAILWAY_RUN_UID=0`: the image runs as a non-root user, and that is Railway's documented
     setting for writing to a volume.
   - A service with a volume runs a single replica and has a short pause on redeploy. That is fine
     here.
4. **Optional:** deploy by tag.
   - Turn off auto-deploy in Railway.
   - Add the `RAILWAY_TOKEN` secret and the `RAILWAY_SERVICE` variable in GitHub.
   - Push a `v*` tag (`.github/workflows/deploy.yml`).

## Failure handling

| Dependency | Circuit breaker | Retries |
|---|---|---|
| LinkedIn search | Opens after 3 failures, or at once on 429/999. Waits 5 min, doubling up to 60 min, and honors `Retry-After` | None inside a cycle; the next cycle is the retry |
| Resend | Opens after 5 failures, for 60 s | 3 attempts on network errors, 429 and 5xx. The `Idempotency-Key` makes a repeat safe |
| Evolution | Opens after 5 failures, for 30 s | 3 attempts, only when the request never left (connection error) or on 5xx. A read timeout is never retried, because the message may have gone |

Before sending, the worker checks `GET /instance/connectionState/<instance>`. When the phone is not
connected, it goes straight to email. WhatsApp also has its own cap of 6 messages an hour; over that,
email takes over.

## Security

- **Secrets come only from the environment.** `.env` is git-ignored, and `.env.example` holds names only.
- **Keys travel only in headers**, never in URLs. The httpx logger is set to WARNING, so request URLs are
  not logged.
- **Logs never contain headers or bodies.** Email addresses and phone numbers are masked
  (`m***@example.com`, `5511*****9999`).
- **Job data is untrusted.** Each job is validated by a schema with bounded lengths, the text is
  HTML-escaped in emails, and links are rebuilt from the id.
- **The state file keeps only job ids and dates.** No titles, companies or recruiters.
- **Secret scanning:** gitleaks runs in CI (`.github/workflows/ci.yml`) and as a pre-commit hook
  (`pip install pre-commit && pre-commit install`).
- **No inbound HTTP**, so CORS, cookies and Bearer auth do not apply. If an endpoint is ever added,
  [SCOPE.md](SCOPE.md), section 5, lists the pattern to copy for each.

## Commands

| Command | What it does |
|---|---|
| `make install` | Create the venv and install dependencies |
| `make run` | Run the worker |
| `make check` | ruff + mypy (strict) + pytest |
| `make format` | ruff format |
| `make lock` | Regenerate `requirements.txt` from `requirements.in` |
