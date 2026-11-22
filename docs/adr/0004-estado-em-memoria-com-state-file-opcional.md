---
status: accepted
type: software
---

# 4. Estado em memória, com `STATE_FILE` opcional

## Context

O worker precisa lembrar quais vagas já tratou, para não avisar duas vezes nem gastar a cota da IA de novo. É uma
réplica só, e o único dado a guardar é "esta vaga já foi vista".

## Decision

O conjunto de vistos fica em memória. Com `STATE_FILE` (um volume no Railway), ele é gravado num arquivo JSON,
escrito num temporário e renomeado. O arquivo guarda só `fonte:id` e a data, mais as baselines das buscas.

## Alternatives

- **Só memória:** zero custo, mas um redeploy esquece tudo e as vagas publicadas na pausa se perdem.
- **Redis ou banco:** mais um serviço para manter, sem nada que o arquivo não faça para uma réplica.

## Trade-offs

- **We gain:** restart sem reenviar vagas, com um volume e uma variável.
- **We pay:** uma réplica só, e uma pausa curta no redeploy.
- **The rejected option would cost:** um serviço a mais (Redis) ou avisos repetidos a cada deploy (só memória).

## Consequences

- Nada de título, empresa ou recrutador no estado: guardar o mínimo (LGPD, art. 7º).
- Os ids ficam 1 ano (`MAX_AGE`): o resumo lê vagas de qualquer data, e um prazo curto faria vaga antiga voltar.
- Rever se o projeto passar a ter painel, vários usuários ou mais de uma réplica.
