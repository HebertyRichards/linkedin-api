---
status: accepted
type: software
---

# 10. PDF do currículo com fpdf2

## Context

O currículo ajustado vai como PDF para as fontes que pedem arquivo. Sistemas de triagem (ATS) leem melhor um PDF
de uma coluna, com texto real. O worker roda em contêiner no Railway e também no Windows, em desenvolvimento.

## Decision

`fpdf2`, com uma coluna e a fonte embutida. O PDF tem data de criação fixa: o mesmo currículo dá os mesmos bytes, e
a mesma chave de idempotência no Resend.

## Alternatives

- **WeasyPrint (HTML para PDF):** precisa de Pango/GTK no sistema, não roda no Windows sem instalar isso e engorda a
  imagem.

## Trade-offs

- **We gain:** dependência só de Python, imagem pequena e PDF determinístico.
- **We pay:** a fonte embutida só tem Latin-1: travessão, reticências e aspas curvas viram ASCII, e traço ou espaço
  Unicode vira `-` ou espaço (`_latin1` em `services/resume_pdf.py`).
- **The rejected option would cost:** dependências de sistema e uma imagem maior.

## Consequences

- Texto novo no PDF passa por `_latin1`; um caractere fora dele sairia como "?".
- Rever se o currículo precisar de layout que o fpdf2 não faz bem, ou de caracteres fora do Latin-1.
