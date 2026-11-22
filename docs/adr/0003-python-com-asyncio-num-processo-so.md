---
status: accepted
type: software
---

# 3. Python com asyncio, num processo só

## Context

O worker passa quase todo o tempo esperando rede: fontes, IA e canais. Peças prontas e testadas já existiam em
Python (circuit breaker, cliente da Evolution, envio pelo Resend, settings que falham sem ecoar valores, logging
JSON), com ruff, mypy e Makefile configurados.

## Decision

Python 3.13+, `httpx.AsyncClient` compartilhado, pydantic nas bordas e mypy em modo `strict`. Um único event loop
numa única réplica: breakers, conjunto de vistos e o teto do WhatsApp ficam em memória, sem lock.

## Alternatives

- **TypeScript strict (Node 24):** a mesma checagem no CI e menos dependências, mas todas as peças seriam
  reescritas.
- **Java (Spring Boot 4.1 ou puro):** pesado demais para um loop só.

## Trade-offs

- **We gain:** reaproveitamento das peças prontas e código simples, sem concorrência entre processos.
- **We pay:** os tipos só valem no CI; fora das bordas do pydantic, nada barra um tipo errado em produção.
- **The rejected option would cost:** reescrever e testar de novo tudo o que já funcionava.

## Consequences

- Toda chamada de rede é async e toda escrita de arquivo passa por `asyncio.to_thread` (`AGENTS.md`).
- Estado sem lock exige que não haja `await` entre ler e gravar o mesmo estado.
- Duas réplicas quebrariam o conjunto de vistos e os limites: rever se um dia precisar escalar.
