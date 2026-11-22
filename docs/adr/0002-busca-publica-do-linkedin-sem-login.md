---
status: accepted
type: software
---

# 2. Busca pública do LinkedIn, sem login

## Context

O LinkedIn não tem API para ler vagas de outras empresas. Para quem não é parceiro só existem `profile`, `email` e
`w_member_social`; os produtos de vagas (Job Posting API, Apply with LinkedIn) só enviam dados *para* o LinkedIn
e estão fechados a novos parceiros; o Apply Connect é para ATS com clientes; os webhooks só falam das vagas do
próprio parceiro. Nenhum avisa "vaga nova publicada" ([getting-access], [job-posting], [apply-connect]).

Os termos proíbem leitura automática: User Agreement §8.2 ("scripts, robots… to scrape", inclusive "through third
parties (such as… data aggregators)"), API Terms §3.1, e o `robots.txt` traz `Disallow: /`. Os processos do
LinkedIn (hiQ, Proxycurl, ProAPIs) tiveram em comum contas falsas e revenda de dados.

O aviso precisa chegar em poucos minutos depois de a vaga aparecer na busca.

## Decision

Ler a busca pública `jobs-guest` (a mesma página que um visitante sem conta vê), com um User-Agent próprio,
**sem login, sem cookie e sem se passar por navegador**, e comparar os ids a cada ciclo. O evento de vaga nova é
gerado pelo próprio worker. A fonte fica atrás do contrato `JobSource`, então trocar de fonte é trocar um arquivo.

## Alternatives

- **Apify (`advanced-linkedin-job-search-api`):** a vaga chega ~1h depois e custa ~US$ 32/mês com 3 buscas por
  hora; os termos valem também "through third parties", e o fornecedor pode sumir (como o Proxycurl).
- **Job Alert do LinkedIn lido do email:** é diário ou semanal, e a senha de app IMAP abre a caixa inteira.

## Trade-offs

- **We gain:** aviso no ciclo seguinte à vaga aparecer, custo zero e nenhuma credencial do LinkedIn.
- **We pay:** o risco de termos é direto; o parser de HTML quebra quando a página muda; IP de datacenter recebe
  429 e bloqueio.
- **The rejected option would cost:** atraso de 1h e mensalidade (Apify), ou atraso de um dia e uma credencial
  ampla (email).

## Consequences

- Nunca fazer login nem reaproveitar cookie do LinkedIn: scraping logado com contas falsas foi o que levou os
  casos acima ao processo, e põe a conta de quem usa em risco (`AGENTS.md`, regra 7).
- O local vai em inglês: `location=Brasil` não é reconhecido e volta vaga dos EUA; `Brazil` e `São Paulo` funcionam.
- A busca não ordena por data (nem com `sortBy=DD`): o ciclo lê a janela da última hora (`f_TPR=r3600`) inteira,
  até 3 páginas de 10, em vez de confiar na primeira página.
- Mitigações de bloqueio no [CONTEXT.md](../../CONTEXT.md) (breaker, espaçamento, aviso de fonte caída).
- Rever se o LinkedIn abrir uma API de leitura, ou se o bloqueio ficar permanente: aí o Apify entra como reserva
  atrás do mesmo contrato, com o atraso de 1h.

[getting-access]: https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access
[job-posting]: https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview
[apply-connect]: https://learn.microsoft.com/en-us/linkedin/talent/apply-connect/apply-connect-overview
