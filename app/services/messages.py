import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape

from app.channels.base import Message
from app.schemas.job import Assessment, Job

# Up to this many new jobs in one cycle go out one message each; more than that are grouped.
SINGLE_LIMIT = 3
MAX_PER_MESSAGE = 10


def batches(jobs: Sequence[Job]) -> list[list[Job]]:
    if len(jobs) <= SINGLE_LIMIT:
        return [[job] for job in jobs]
    return [list(jobs[i : i + MAX_PER_MESSAGE]) for i in range(0, len(jobs), MAX_PER_MESSAGE)]


def _details(job: Job) -> str:
    return " · ".join(part for part in (job.company, job.location) if part)


def _score_lines(assessment: Assessment | None) -> list[str]:
    if assessment is None:
        return []
    lines = [f"Nota {assessment.score}/100: {assessment.reasons}"]
    if assessment.gaps:
        lines.append("Falta: " + ", ".join(assessment.gaps))
    return lines


def job_message(jobs: Sequence[Job], assessments: Mapping[str, Assessment] | None = None) -> Message:
    """No clock in the text: a repeat of the same jobs and scores gives the same body and key."""
    scores = assessments or {}
    keys = tuple(job.key for job in jobs)

    if len(jobs) == 1:
        job = jobs[0]
        score = f" ({scores[job.key].score}/100)" if job.key in scores else ""
        subject = f"Nova vaga Easy Apply{score}: {job.title}" + (f" · {job.company}" if job.company else "")
        heading = "Nova vaga (Easy Apply)"
    else:
        subject = f"{len(jobs)} novas vagas Easy Apply"
        heading = f"{len(jobs)} novas vagas (Easy Apply)"

    text_items = [
        "\n".join(
            line for line in (f"*{job.title}*", _details(job), *_score_lines(scores.get(job.key)), job.url) if line
        )
        for job in jobs
    ]
    html_items = [
        f'<li><a href="{job.url}">{escape(job.title)}</a><br>{escape(_details(job))}'
        + "".join(f"<br>{escape(line)}" for line in _score_lines(scores.get(job.key)))
        + "</li>"
        for job in jobs
    ]
    text = f"*{heading}*\n\n" + "\n\n".join(text_items)
    # The body is part of the key: a new score for the same jobs is a new message, never a 409 from Resend.
    digest = hashlib.sha256((",".join(sorted(keys)) + text).encode()).hexdigest()[:32]
    return Message(
        subject=subject,
        text=text,
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
