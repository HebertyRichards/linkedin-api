"""The tailored resume as text to paste into a Gupy profile. Gupy's ranking reads only the typed Experiência and
Habilidades fields (not the PDF) and has no projects field, so every project goes in as an experience item,
labelled for what it is: a client's name for freelance work, "Projeto próprio" for a personal one."""

from app.schemas.resume import Project, Resume, TailoredResume
from app.services.resume_pdf import month_label

# The skills field takes up to 30, as seen on its form (not published by Gupy).
MAX_SKILLS = 30
_MISSING = "(preencha: o currículo não tem a data)"


def _item(empresa: str, cargo: str, inicio: str | None, fim: str | None, lines: list[str]) -> str:
    period = (
        [f"Início: {month_label(inicio)}", f"Fim: {month_label(fim)}"]
        if inicio
        else [f"Início: {_MISSING}", f"Fim: {_MISSING}"]
    )
    return "\n".join([f"Empresa: {empresa}", f"Cargo: {cargo}", *period, "Descrição:", *lines])


def _project(project: Project, title: str) -> str:
    lines = [f"- {item.texto}" for item in project.destaques]
    if project.tecnologias:
        lines.append(f"Tecnologias: {', '.join(project.tecnologias)}")
    if project.tipo == "freelance":
        empresa = project.cliente or "Freelancer (autônomo)"
        cargo = project.cargo or f"{title} (freelance)"
        lines.insert(0, f"Projeto freelance: {project.nome}")
    else:
        empresa = f"Projeto próprio: {project.nome}"
        cargo = project.cargo or title
    return _item(empresa, cargo, project.inicio, project.fim, lines)


def gupy_resume(resume: Resume, tailored: TailoredResume) -> TailoredResume:
    """Only what Gupy's ranking reads: no summary (no field for it), the skills the field takes, and every project,
    not only the ones the PDF keeps: each one is more proof in the field the ranking reads. The ones the tailoring
    kept come first, with their highlights in its order."""
    kept = {project.id for project in tailored.projetos}
    projects = [*tailored.projetos, *(p for p in resume.projetos if p.id not in kept)]
    return tailored.model_copy(
        update={"resumo": "", "projetos": projects, "habilidades": tailored.habilidades[:MAX_SKILLS]}
    )


def gupy_text(resume: Resume, tailored: TailoredResume, label: str) -> str:
    tailored = gupy_resume(resume, tailored)
    projects = tailored.projetos
    jobs = [
        _item(
            job.empresa,
            job.cargo,
            job.inicio,
            job.fim,
            [f"- {item.texto}" for item in job.destaques]
            + ([f"Tecnologias: {', '.join(job.tecnologias)}"] if job.tecnologias else []),
        )
        for job in tailored.experiencias
    ]
    blocks = [
        f"{label}\nTexto para colar na Gupy. A IA dela só lê Experiência e Habilidades: cada bloco abaixo é um "
        "item de Experiência (projetos e freelances incluídos).",
        "EXPERIÊNCIA",
        *jobs,
        *(_project(project, resume.titulo) for project in projects),
        f"HABILIDADES (as {MAX_SKILLS} primeiras, da mais ligada à vaga)",
        ", ".join(tailored.habilidades),
    ]
    return "\n\n".join(blocks) + "\n"
