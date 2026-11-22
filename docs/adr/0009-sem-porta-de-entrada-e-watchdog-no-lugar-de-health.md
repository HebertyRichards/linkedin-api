---
status: accepted
type: software
---

# 9. Sem porta de entrada; watchdog no lugar de `/health`

## Context

O worker só faz chamadas de saída. O healthcheck HTTP do Railway roda só no deploy ("does not monitor the
healthcheck endpoint after the deployment has gone live"), e o que o Railway faz sempre é reiniciar quem sai com
erro (política On Failure). Um loop travado sem sair passaria despercebido.

## Decision

Nenhum servidor HTTP, nenhum `EXPOSE` e nenhuma rota. Uma thread (`utils/watchdog.py`) confere o batimento do loop
a cada minuto; sem batimento por `max(intervalo + 20%, 1h) + 1h`, loga `HEALTH_STALLED` e sai com código 1, e o
Railway reinicia.

## Alternatives

- **Rota `/health`:** só seria checada no deploy, e abriria a primeira porta do projeto.
- **Tarefa async no lugar da thread:** não dispara se o event loop travar inteiro.

## Trade-offs

- **We gain:** nenhuma superfície de entrada (sem CORS, cookie, rate limit de entrada nem autenticação a manter), e
  restart automático num travamento.
- **We pay:** um travamento leva até ~2h para ser notado com o intervalo padrão de 30 min.
- **The rejected option would cost:** uma porta pública que não detecta o travamento depois do deploy.

## Consequences

- As chaves são o ponto de entrada de verdade: ficam só nas Variables do Railway e no `.env` local.
- Se surgir painel ou rota: allowlist de CORS sem `*`, cookie HttpOnly/Secure/SameSite com CSRF, comparação de
  token com `hmac.compare_digest` e rate limit por IP real.
- O loop bate antes de cada etapa e antes de dormir; uma etapa lenta (10 vagas com a IA no limite por minuto) cabe
  na margem.
