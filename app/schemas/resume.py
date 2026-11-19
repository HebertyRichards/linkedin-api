from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class _Model(BaseModel):
    # Unknown keys are dropped: `contato` is never read here, so it cannot reach a prompt.
    model_config = ConfigDict(frozen=True, extra="ignore")


class Highlight(_Model):
    id: str
    texto: str
    skills: list[str] = []


class Experience(_Model):
    id: str
    empresa: str
    cargo: str
    inicio: str
    fim: str | None = None
    tecnologias: list[str] = []
    destaques: list[Highlight]


class Project(_Model):
    id: str
    nome: str
    fixo: bool = False
    # Gupy has no projects field and its ranking reads only Experiência and Habilidades, so the Gupy text lists
    # each project as an experience item: these fill its Empresa, Cargo and period.
    tipo: Literal["pessoal", "freelance"] = "pessoal"
    cliente: str = ""
    cargo: str = ""
    inicio: str | None = None
    fim: str | None = None
    tecnologias: list[str] = []
    destaques: list[Highlight]


class Education(_Model):
    curso: str
    instituicao: str
    periodo: str
    status: str


class SearchProfile(_Model):
    niveis: list[str]
    modelos_trabalho: list[str]


class Resume(_Model):
    """The resume without contact data: the only part an LLM ever sees."""

    titulo: str
    idiomas: list[str]
    perfil_busca: SearchProfile
    resumo: str
    experiencias: list[Experience]
    projetos: list[Project]
    formacao: list[Education]
    habilidades: dict[str, list[str]]
    habilidades_sob_demanda: list[str] = []

    def prompt_text(self, today: date) -> str:
        """Compact plain text for the prompt (fewer tokens than the YAML)."""
        lines = [
            f"Título: {self.titulo}",
            f"Níveis buscados: {', '.join(self.perfil_busca.niveis)}",
            f"Idiomas: {', '.join(self.idiomas)}",
            f"Resumo: {self.resumo}",
            "Experiência:",
        ]
        for job in self.experiencias:
            end = job.fim or f"atual ({today:%Y-%m})"
            lines.append(f"- {job.cargo} na {job.empresa}, {job.inicio} a {end}: {', '.join(job.tecnologias)}")
            lines.extend(f"  * {item.texto}" for item in job.destaques)
        lines.append("Projetos:")
        for project in self.projetos:
            lines.append(f"- {project.nome}: {', '.join(project.tecnologias)}")
            lines.extend(f"  * {item.texto}" for item in project.destaques)
        lines.append("Formação:")
        lines.extend(f"- {e.curso}, {e.instituicao}, {e.periodo} ({e.status})" for e in self.formacao)
        skills = self.habilidades | {"sob demanda": self.habilidades_sob_demanda}
        lines.append("Habilidades: " + "; ".join(f"{group}: {', '.join(items)}" for group, items in skills.items()))
        return "\n".join(lines)


class Contact(_Model):
    """Printed on the PDF only. Kept apart from Resume so no prompt can include it."""

    nome: str
    telefone: str = ""
    email: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    cidade: str = ""


class _ContactBlock(_Model):
    contato: Contact


class TailoredResume(_Model):
    """The resume for one job, assembled by code from the base: only the order, the pick and the summary change.
    Companies, roles, dates and education always come from the base, never from the LLM."""

    resumo: str
    experiencias: list[Experience]
    projetos: list[Project]
    habilidades: list[str]


def read_resume_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"RESUME_FILE could not be read: {type(e).__name__}") from None


def load_resume(path: Path) -> Resume:
    return parse_resume(read_resume_file(path))


def parse_resume(text: str) -> Resume:
    """Read once at boot. Errors name the problem, never the content (it is personal data)."""
    return _parse(Resume, text)


def parse_contact(text: str) -> Contact:
    return _parse(_ContactBlock, text).contato


def _parse[M: _Model](model: type[M], text: str) -> M:
    try:
        return model.model_validate(yaml.safe_load(text))
    except yaml.YAMLError:
        raise RuntimeError("the resume is not valid YAML") from None
    except ValidationError as e:
        fields = ", ".join(".".join(str(p) for p in err["loc"]) for err in e.errors())
        raise RuntimeError(f"the resume is missing or has invalid fields: {fields}") from None
