---
status: accepted
type: process
---

# 1. Registrar decisões com os seus custos

## Context

Decisão difícil de desfazer é esquecida, e a próxima pessoa (ou agente) a "corrige" sem saber por que ela foi
tomada. Anotar toda escolha pequena enterra as poucas que importam.

## Decision

Uma decisão ganha um registro em `docs/adr/NNNN-slug.md` só quando as três condições valem:

1. **Difícil de desfazer:** mudar depois custa tempo ou dinheiro de verdade.
2. **Surpreende sem contexto:** quem lê o código perguntaria "por que foi feito assim?".
3. **Troca real:** havia alternativas reais, e uma foi escolhida por motivos concretos.

Regra de negócio vai para o [CONTEXT.md](../../CONTEXT.md); o que o projeto faz e não faz, para o
[SCOPE.md](../../SCOPE.md). Preferência de estilo, biblioteca fácil de trocar e o que o linter cobra não viram
registro. Registro aceito não é editado: um novo o substitui, e o antigo recebe `status: superseded by ADR-NNNN`.

## Alternatives

- **Sem registros:** não custa nada agora, mas os motivos vão embora com quem os conhecia.
- **Registrar tudo:** completo, mas as decisões que importam se perdem entre as triviais.

## Trade-offs

- **We gain:** o motivo de cada escolha difícil de desfazer sobrevive, para pessoas e agentes.
- **We pay:** alguns minutos por decisão, e a disciplina de pular as triviais.
- **The rejected options would cost:** debates repetidos e reversões acidentais, ou ruído que ninguém lê.

## Consequences

Agentes leem `docs/adr/` antes de mudar algo que um registro cobre (ver `AGENTS.md`). Rever este registro se a
pasta crescer para dezenas de registros que ninguém lê.
