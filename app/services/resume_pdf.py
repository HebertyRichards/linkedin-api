"""The tailored resume as a PDF: one column, real text, built-in font. Recruiters' screening systems (ATS)
read this layout best, and it needs no font file nor system library (it runs the same on Windows and in
the slim Docker image)."""

from datetime import UTC, datetime

from fpdf import FPDF

from app.schemas.resume import Contact, Education, TailoredResume

_MONTHS = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")
# The built-in Helvetica covers Latin-1 (every Portuguese accent) but not these, which LLM text often has.
_TO_LATIN1 = str.maketrans(
    {
        "\u2014": "-",  # em dash
        "\u2013": "-",  # en dash
        "\u2026": "...",  # ellipsis
        "\u2022": "-",  # bullet
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u2192": "->",  # arrow
        "\u00a0": " ",  # no-break space
    }
)
_ACCENT = (40, 90, 160)
# Fixed, so the same resume always gives the same bytes (and the same email idempotency key).
_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _latin1(text: str) -> str:
    return text.translate(_TO_LATIN1).encode("latin-1", "replace").decode("latin-1")


def _month(value: str | None) -> str:
    if not value:
        return "Atual"
    year, _, month = value.partition("-")
    return f"{_MONTHS[int(month) - 1]}/{year}" if month.isdigit() and 1 <= int(month) <= 12 else value


class _Pdf(FPDF):
    def heading(self, text: str) -> None:
        self.ln(3)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*_ACCENT)
        self.cell(0, 7, _latin1(text.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def line_text(self, text: str, *, style: str = "", size: float = 10, height: float = 5) -> None:
        self.set_font("Helvetica", style, size)
        self.multi_cell(0, height, _latin1(text), new_x="LMARGIN", new_y="NEXT")

    def bullet(self, text: str) -> None:
        self.set_font("Helvetica", "", 10)
        self.set_x(self.l_margin + 3)
        self.multi_cell(0, 5, _latin1(f"- {text}"), new_x="LMARGIN", new_y="NEXT")


def render_pdf(
    contact: Contact, title: str, resume: TailoredResume, education: list[Education], languages: list[str]
) -> bytes:
    pdf = _Pdf(format="A4")
    pdf.set_creation_date(_CREATED)
    pdf.set_margins(15, 12, 15)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, _latin1(contact.nome), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.line_text(title, size=11, height=6)
    details = " | ".join(part for part in (contact.telefone, contact.email, contact.cidade) if part)
    links = " | ".join(part for part in (contact.linkedin, contact.github, contact.portfolio) if part)
    for line in (details, links, f"Idiomas: {', '.join(languages)}" if languages else ""):
        if line:
            pdf.line_text(line, size=9, height=4.5)

    pdf.heading("Resumo")
    pdf.line_text(resume.resumo)

    pdf.heading("Experiência")
    for job in resume.experiencias:
        pdf.line_text(f"{job.empresa} - {job.cargo} | {_month(job.inicio)} - {_month(job.fim)}", style="B")
        if job.tecnologias:
            pdf.line_text(f"Tecnologias: {', '.join(job.tecnologias)}", style="I", size=9, height=4.5)
        for item in job.destaques:
            pdf.bullet(item.texto)
        pdf.ln(1)

    pdf.heading("Projetos")
    for project in resume.projetos:
        pdf.line_text(project.nome, style="B")
        if project.tecnologias:
            pdf.line_text(f"Tecnologias: {', '.join(project.tecnologias)}", style="I", size=9, height=4.5)
        for item in project.destaques:
            pdf.bullet(item.texto)
        pdf.ln(1)

    pdf.heading("Formação")
    for course in education:
        pdf.line_text(f"{course.curso} - {course.instituicao} | {course.periodo} ({course.status})")

    pdf.heading("Habilidades")
    pdf.line_text(", ".join(resume.habilidades))
    return bytes(pdf.output())
