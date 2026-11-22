---
status: accepted
type: software
---

# 5. Nota por IA, em API compatível com OpenAI

## Context

Avisar toda vaga da busca gera ruído; o que interessa é a vaga que bate com o currículo. Ler a descrição e
comparar com o currículo pede um modelo de linguagem. O orçamento é zero, e o currículo é dado pessoal.

## Decision

Cada vaga recebe uma nota de 0 a 100 de um modelo de chat no formato OpenAI. O padrão é a Groq, no plano grátis
(`gpt-oss-20b` para a nota, `gpt-oss-120b` para ajustar o currículo: a Groq limita cada modelo à parte). Um
segundo provedor compatível (`LLM_FALLBACK_*`) responde quando o primeiro falha. O código depende do contrato
`ChatModel`, não de um provedor.

## Alternatives

- **Claude Haiku:** ~US$ 1,50/mês; fora do orçamento zero.
- **Gemini grátis:** os termos proíbem enviar dado pessoal no plano grátis.
- **OpenRouter `:free`:** 50 pedidos/dia; serve como reserva, não como principal.
- **Adapter nativo por provedor (Anthropic, Gemini):** fica de fora até alguém usar um.

## Trade-offs

- **We gain:** filtro de relevância sem custo, e troca de provedor por variável de ambiente.
- **We pay:** a cota grátis limita quantas notas cabem por dia; a nota varia alguns pontos entre modelos; o
  provedor recebe o currículo (sem o bloco `contato`).
- **The rejected option would cost:** mensalidade, ou um provedor cujo plano grátis não aceita dado pessoal.

## Consequences

- A IA é obrigatória: sem `LLM_API_KEY` e currículo o worker não sobe, porque sem nota não há o que mandar.
- A resposta da IA é dado não confiável: JSON com schema estrito e validação em `schemas/job.py`.
- As regras da nota e do currículo ajustado estão no [CONTEXT.md](../../CONTEXT.md).
- Rever se a cota grátis não der conta das fontes, ou se o provedor mudar a política de dados.
