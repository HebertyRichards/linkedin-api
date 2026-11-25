import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape

from app.channels.base import Message
from app.schemas.job import SOURCE_LABELS, Assessment, Job, Source, clip
from app.services.tailor import MIN_SCORE, TailoredFile

# Up to this many new jobs in one cycle go out one message each; more than that are grouped.
SINGLE_LIMIT = 3
MAX_PER_MESSAGE = 10
RESUME_DOWN = "Currículo ajustado indisponível no momento."


def batches(jobs: Sequence[Job]) -> list[list[Job]]:
    if len(jobs) <= SINGLE_LIMIT:
        return [[job] for job in jobs]
    return [list(jobs[i : i + MAX_PER_MESSAGE]) for i in range(0, len(jobs), MAX_PER_MESSAGE)]


def _details(job: Job) -> str:
    # The source last: it tells where to apply.
    return " · ".join(part for part in (job.company, job.location, SOURCE_LABELS[job.source]) if part)


def _score_lines(assessment: Assessment | None) -> list[str]:
    if assessment is None:
        return []
    lines = [f"Nota {assessment.score}/100: {assessment.reasons}"]
    if assessment.gaps:
        lines.append("Falta: " + ", ".join(assessment.gaps))
    return lines


def _resume_lines(tailored: TailoredFile | None, assessment: Assessment | None = None) -> list[str]:
    """A preview of what the tailored resume leads with; the full one is the attached file (a Gupy job: the text
    to paste into its form, no PDF)."""
    if tailored is None:
        due = assessment is not None and assessment.score >= MIN_SCORE and assessment.workplace != "onsite"
        return [RESUME_DOWN] if due else []
    resume = tailored.resume
    top = [item.texto for job in resume.experiencias for item in job.destaques][:3]
    gupy = tailored.attachment.media_type == "text/plain"
    kind, held = ("Texto para colar na Gupy", "texto") if gupy else ("Currículo ajustado", "currículo")
    score: list[str] = []
    if (after := tailored.assessment) is not None:
        # The gaps left after tailoring are what the resume itself lacks: what to study or add to it.
        diff = f" ({after.score - assessment.score:+d})" if assessment else ""
        score.append(f"Nota com o {held} ajustado: {after.score}/100{diff}: {after.reasons}")
        if after.gaps:
            score.append("Ainda falta: " + ", ".join(after.gaps))
    return [
        f"{kind} em anexo: {tailored.attachment.filename}",
        *score,
        *([f"Resumo: {clip(resume.resumo, 220)}"] if resume.resumo else []),
        "Destaques: " + "; ".join(clip(text, 90) for text in top),
        "Projetos: " + " + ".join(project.nome for project in resume.projetos),
    ]


# Inline styles only: Gmail and Outlook drop <style> blocks and external CSS.
_CARD = "border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:0 0 12px;font-family:Arial,sans-serif"
_PREVIEW = "background:#f6f8fa;border-radius:6px;padding:8px 12px;margin-top:8px;font-size:13px"
_BUTTON = (
    "display:inline-block;margin-top:10px;padding:6px 12px;background:#1a4d8f;color:#ffffff;"
    "border-radius:6px;text-decoration:none"
)


def _card(job: Job, assessment: Assessment | None, preview: list[str]) -> str:
    score = f'<div style="font-size:13px;color:#1a4d8f"><b>{assessment.score}/100</b></div>' if assessment else ""
    lines = "".join(f'<div style="margin-top:4px">{escape(line)}</div>' for line in _score_lines(assessment))
    resume = (
        f'<div style="{_PREVIEW}"><b>{escape(preview[0])}</b>'
        + "".join(f"<br>{escape(line)}" for line in preview[1:])
        + "</div>"
        if preview
        else ""
    )
    return (
        f'<div style="{_CARD}">{score}'
        f'<a href="{job.url}" style="font-size:16px;font-weight:bold;color:#1f2328">{escape(job.title)}</a>'
        f'<div style="color:#57606a">{escape(_details(job))}</div>{lines}{resume}'
        f'<a href="{job.url}" style="{_BUTTON}">Ver vaga e se candidatar</a></div>'
    )


def _tg(text: str) -> str:
    # Telegram knows only &lt; &gt; &amp; &quot;: the &#x27; that escape() writes for ' is a 400.
    return escape(text, quote=False)


def _telegram_block(job: Job, assessment: Assessment | None, preview: list[str]) -> str:
    href = _tg(job.url).replace('"', "&quot;")
    lines = (
        f"<b>{_tg(job.title)}</b>",
        _tg(_details(job)),
        *map(_tg, _score_lines(assessment)),
        *map(_tg, preview),
        f'<a href="{href}">Ver vaga e se candidatar</a>',
    )
    return "\n".join(line for line in lines if line)


def job_message(
    jobs: Sequence[Job],
    assessments: Mapping[str, Assessment] | None = None,
    tailored: Mapping[str, TailoredFile] | None = None,
    *,
    heading: str | None = None,
) -> Message:
    """No clock in the text: a repeat of the same jobs, scores and files gives the same body and key.
    `heading`: a list on request (/vagas), with that title instead of "new jobs" and no resume lines."""
    scores = assessments or {}
    files = tailored or {}
    keys = tuple(job.key for job in jobs)
    listing = heading is not None  # read before the default heading below is set

    def preview(job: Job) -> list[str]:
        return [] if listing else _resume_lines(files.get(job.key), scores.get(job.key))

    if heading is not None:
        subject = heading
    elif len(jobs) == 1:
        job = jobs[0]
        score = f" ({scores[job.key].score}/100)" if job.key in scores else ""
        subject = f"Nova vaga{score}: {job.title}" + (f" · {job.company}" if job.company else "")
        heading = "Nova vaga"
    else:
        subject = heading = f"{len(jobs)} novas vagas"

    text_items = [
        "\n".join(
            line
            for line in (
                f"*{job.title}*",
                _details(job),
                *_score_lines(scores.get(job.key)),
                *preview(job),
                job.url,
            )
            if line
        )
        for job in jobs
    ]
    text = f"*{heading}*\n\n" + "\n\n".join(text_items)
    attachments = tuple(files[job.key].attachment for job in jobs if job.key in files)
    # Body and files are part of the key: a new score for the same jobs is a new message, never a 409 from Resend.
    fingerprint = hashlib.sha256((",".join(sorted(keys)) + text).encode())
    for attachment in attachments:
        fingerprint.update(attachment.content)
    return Message(
        subject=subject,
        text=text,
        html=f'<h2 style="font-family:Arial,sans-serif">{heading}</h2>'
        + "".join(_card(job, scores.get(job.key), preview(job)) for job in jobs),
        telegram_blocks=(
            f"<b>{heading}</b>",
            *(_telegram_block(job, scores.get(job.key), preview(job)) for job in jobs),
        ),
        idempotency_key=f"jobs-{fingerprint.hexdigest()[:32]}",
        keys=keys,
        attachments=attachments,
    )


def source_down_message(since: datetime, source: Source) -> Message:
    when = since.strftime("%d/%m %H:%M UTC")
    label = SOURCE_LABELS[source]
    text = (
        f"A busca de vagas do {label} está falhando desde {when}. "
        f"Nenhuma vaga nova do {label} será avisada até ela voltar; veja os logs do worker."
    )
    return Message(
        subject=f"Alerta: busca de vagas do {label} falhando",
        text=f"*Alerta do linkedin-api*\n\n{text}",
        html=f"<h2>Alerta do linkedin-api</h2><p>{escape(text)}</p>",
        telegram_blocks=("<b>Alerta do linkedin-api</b>", _tg(text)),
        idempotency_key=f"source-down-{source}-{int(since.timestamp())}",
        keys=(),
    )
