---
status: accepted
type: software
---

# 7. Várias fontes públicas, sem filtro Easy Apply

## Context

Com o filtro Easy Apply, só o LinkedIn servia: Easy Apply é um recurso dele, que ATS e agregadores não conhecem.
Sem o filtro, qualquer vaga com candidatura serve, e fontes brasileiras entram. As pesquisadas:

- **Sem API de leitura:** Indeed (Publisher API aposentada), Glassdoor (fechada), Catho (só para empresas),
  InfoJobs (a API é a da Espanha e pede credencial), Vagas.com e Sólides (só HTML ou token).
- **Responderam JSON público, sem chave, login nem cookie:** Gupy (`__NEXT_DATA__` de `portal.gupy.io`),
  Remotar, Himalayas e Workable.

## Decision

Toda vaga da busca, com qualquer tipo de candidatura (o worker continua sem se candidatar). Cinco fontes:
LinkedIn, Gupy, Remotar, Himalayas e Workable, todas lidas em todo ciclo, ao mesmo tempo. Cada uma tem breaker
próprio e aviso próprio de queda. A chave da vaga é `fonte:id`.

## Alternatives

- **Manter Easy Apply e só o LinkedIn:** menos vagas e uma fonte só para bloquear.
- **Deduplicar a mesma vaga entre fontes por título e empresa:** descartado; a repetição fica como limite aceito.
- **Remotive, RemoteOK, Jobicy, We Work Remotely e Trampos:** responderam sem chave, mas ficaram de fora; o
  Trampos trouxe poucas vagas.
- **Servidor MCP para consultar vagas:** o `/vagas` do Telegram já cobre isso, e um MCP remoto abriria uma porta.

## Trade-offs

- **We gain:** mais vagas, e uma fonte bloqueada não cala as outras.
- **We pay:** mais notas da cota grátis da IA; mais parsers de páginas não oficiais, cada um com risco de termos e
  de mudar sem aviso; a mesma vaga em duas fontes chega duas vezes.
- **The rejected option would cost:** depender só do LinkedIn, que bloqueia IP de datacenter.

## Consequences

- Nova fonte segue o molde de `sources/base.py` (`AGENTS.md`, "New source").
- Resposta sem vaga reconhecida conta como falha (layout mudou), nunca como "0 vagas".
- O link é montado do id; na Gupy, o subdomínio de carreira vem da página só depois de validado como um único
  rótulo DNS sob `gupy.io`.
- Rever uma fonte quando ela passar a pedir chave, mudar o formato ou começar a bloquear.
