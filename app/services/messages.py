import hashlib
from collections.abc import Sequence
from datetime import datetime
from html import escape

from app.channels.base import Message
from app.schemas.job import Job

# Up to this many new jobs in one cycle go out one message each; more than that are grouped.
SINGLE_LIMIT = 3
MAX_PER_MESSAGE = 10


def batches(jobs: Sequence[Job]) -> list[list[Job]]:
    if len(jobs) <= SINGLE_LIMIT:
        return [[job] for job in jobs]
    return [list(jobs[i : i + MAX_PER_MESSAGE]) for i in range(0, len(jobs), MAX_PER_MESSAGE)]


def _details(job: Job) -> str:
    return " · ".join(part for part in (job.company, job.location) if part)


def job_message(jobs: Sequence[Job]) -> Message:
    """Text is built only from stable fields: a repeat of the same jobs gives the same body and key."""
    keys = tuple(job.key for job in jobs)
    digest = hashlib.sha256(",".join(sorted(keys)).encode()).hexdigest()[:32]

    if len(jobs) == 1:
        job = jobs[0]
        subject = f"Nova vaga Easy Apply: {job.title}" + (f" · {job.company}" if job.company else "")
        heading = "Nova vaga (Easy Apply)"
    else:
        subject = f"{len(jobs)} novas vagas Easy Apply"
        heading = f"{len(jobs)} novas vagas (Easy Apply)"

    text_items = ["\n".join(line for line in (f"*{job.title}*", _details(job), job.url) if line) for job in jobs]
    html_items = [f'<li><a href="{job.url}">{escape(job.title)}</a><br>{escape(_details(job))}</li>' for job in jobs]
    return Message(
        subject=subject,
        text=f"*{heading}*\n\n" + "\n\n".join(text_items),
        html=f"<h2>{heading}</h2><ul>{''.join(html_items)}</ul>",
        idempotency_key=f"jobs-{digest}",
        keys=keys,
    )


def source_down_message(since: datetime) -> Message:
    when = since.strftime("%d/%m %H:%M UTC")
    text = (
        f"A busca de vagas está falhando desde {when}. "
        "Nenhuma vaga nova será avisada até ela voltar; veja os logs do worker."
    )
    return Message(
        subject="Alerta: busca de vagas falhando",
        text=f"*Alerta do linkedin-api*\n\n{text}",
        html=f"<h2>Alerta do linkedin-api</h2><p>{escape(text)}</p>",
        idempotency_key=f"source-down-{int(since.timestamp())}",
        keys=(),
    )
