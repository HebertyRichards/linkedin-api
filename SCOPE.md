# linkedin-api — escopo

Worker que detecta **vaga nova com candidatura pelo próprio LinkedIn (Easy Apply)** para
buscas configuradas e envia **um aviso por email (Resend) ou WhatsApp (Evolution API)**.
Pelo menos um dos dois canais precisa aceitar cada aviso.

**Latência alvo:** o aviso chega em até ~1-2 min depois de a vaga aparecer na busca pública do
LinkedIn. O worker não controla quanto o LinkedIn demora para mostrar a vaga na busca depois de
publicada [I].

Não é uma API que lista vagas, não tem painel e não tem banco obrigatório.

Pesquisa feita em 2026-10-24. Nas tabelas, **[V]** = visto na fonte citada e **[I]** = inferido.
O que estiver [I] precisa ser confirmado na implementação.

---

## 1. O que a pesquisa mudou no pedido

### 1.1 Não existe API oficial do LinkedIn para isso [V]

- **Permissões abertas.** Para quem não é parceiro, só existem `profile`, `email` e
  `w_member_social`. O resto "requires explicit approval".
  ([getting-access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access))
- **Produtos de vagas.** Todos enviam dados *para* o LinkedIn: o parceiro publica as vagas dos
  próprios clientes. Os únicos GETs leem o status dessas vagas.
  ([job posting status](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/check-job-posting-status))
- **Programas fechados a novos parceiros.** Job Posting API ("not accepting new partnerships")
  e Apply with LinkedIn ("not accepting any new partners").
  ([job posting](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview),
  [apply with linkedin](https://learn.microsoft.com/en-us/linkedin/talent/apply-with-linkedin/apply-with-linkedin))
- **Apply Connect.** É o único programa aberto, e é para empresas de ATS com clientes. Mesmo
  aprovado, você não recebe as vagas de outras empresas.
  ([apply connect](https://learn.microsoft.com/en-us/linkedin/talent/apply-connect/apply-connect-overview))
- **Webhooks.** Só para apps com caso de uso aprovado, e só sobre as vagas do próprio parceiro.
  Nenhum avisa "vaga nova publicada no LinkedIn".
  ([webhook validation](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/webhook-validation))

**Consequência:** o "evento de vaga nova" não chega por webhook. Quem gera o evento é o próprio
worker: ele consulta a fonte de tempos em tempos e compara os ids das vagas.

### 1.2 Toda fonte que sabe o que é Easy Apply viola os termos do LinkedIn [V]

**O que dizem os termos:**
- **User Agreement §8.2** proíbe "scripts, robots… to scrape" e "bots or other unauthorized
  automated methods". Também proíbe distribuir dados obtidos "directly or through third parties
  (such as… data aggregators)".
  ([user agreement](https://www.linkedin.com/legal/user-agreement))
- **API Terms §3.1** proíbe conteúdo obtido por scraping, "directly or indirectly through a
  third party".
  ([api terms](https://www.linkedin.com/legal/l/api-terms-of-use))
- **robots.txt** traz `User-agent: * Disallow: /`.

**Histórico de ações do LinkedIn:**
- hiQ: acordo judicial de US$ 500 mil (2022).
- Proxycurl: processado em 2025 e encerrado em julho de 2025.
- ProAPIs: processado em outubro de 2025.

O que os três casos têm em comum: contas falsas e revenda de dados. Fontes:
[proskauer](https://www.proskauer.com/blog/hiq-and-linkedin-reach-proposed-settlement-in-landmark-scraping-case),
[socialmediatoday](https://www.socialmediatoday.com/news/linkedin-wins-legal-case-data-scrapers-proxycurl/756101/),
[bloomberglaw](https://news.bloomberglaw.com/artificial-intelligence/linkedin-strikes-deal-to-settle-scraping-battle-with-startup).

**ATS e agregadores não servem.** Greenhouse, Lever, Ashby, Adzuna e Jooble não sabem se a vaga
tem Easy Apply, porque esse é um recurso do LinkedIn.

**Consequência:** a escolha não é "com ou sem risco". É *quem* corre o risco e *quanto custa*.
Ver a decisão D1.

### 1.3 Webhook e WebSocket não são necessários

- **Entrada:** não há push de vaga (1.1).
- **Saída:** a mensagem vai pela API do Resend ou da Evolution.

Webhook ou WebSocket só voltam ao escopo se:
- a fonte passar a oferecer push;
- você quiser confirmação de entrega real, pelos webhooks de status do Resend ou da Evolution;
- surgir um painel ao vivo;
- outros sistemas precisarem consumir os eventos do worker.

### 1.4 Observado na implementação (2026-10-25, requisições reais ao endpoint público)

- **Acesso:** respondeu 200 com um User-Agent próprio (`linkedin-api-job-alert/0.1`), sem login e sem
  se passar por navegador.
- **Local em português é ignorado:** `location=Brasil` não é reconhecido e volta vaga dos EUA. Com
  `Brazil` ou `São Paulo`, as vagas são do Brasil. O `JOB_LOCATION` precisa estar em **inglês**.
- **Sem ordem por data:** os resultados vêm misturados, mesmo com `sortBy=DD` (8h, 16h, 14h...). Por isso
  o worker lê a janela inteira da última hora (`f_TPR=r3600`), com até 3 páginas de 10 vagas, e compara
  ids. Ele não confia na primeira página.
- **A janela de 1h é aplicada:** de madrugada voltou 0 vaga, enquanto a busca de 24h trazia várias.
- **O filtro Easy Apply é respeitado** nas amostras: o detalhe de uma vaga filtrada traz
  `public_jobs_apply-link-simple_onsite`, que é a candidatura dentro do LinkedIn. Foi uma amostra só.

---

## 2. Decisões (tomadas: B, Python, memória com `STATE_FILE` opcional)

### D1. Fonte de vagas

As três opções cabem atrás da mesma interface `JobSource`. Trocar de fonte é trocar um arquivo.

| | A. Apify (`fantastic-jobs/advanced-linkedin-job-search-api`) | B. Endpoint público `jobs-guest` direto | C. Job Alert do LinkedIn lido do seu email |
|---|---|---|---|
| **Avisa em ~1-2 min?** | **não**: janela de 1h, vagas chegam ~1h depois [V] | **sim**: consulta a cada 1-2 min, lendo a janela de 1h (seção 1.4) | **não**: diário ou semanal [V] |
| Easy Apply | campo `direct_apply` [V] | filtro `f_AL=true` na URL [I, só terceiros documentam] | filtro oficial na busca [V]; se o alerta herda o filtro [I] |
| Custo | ~US$ 32/mês com 3 buscas por hora [I, cálculo]; a cada minuto, a taxa por execução torna inviável | zero | zero |
| Credencial | `APIFY_TOKEN` | nenhuma | acesso à sua caixa de email (senha de app IMAP = **caixa inteira**) |
| Quem roda o scraping | o fornecedor | você | ninguém |
| Riscos | termos valem "through third parties"; o fornecedor pode sumir (Proxycurl) | violação direta; ~1.440 requisições/dia por busca, a partir de IP de datacenter do Railway, aumenta a chance de 429 e bloqueio [I]; mudança no HTML quebra o parser | credencial ampla |

**Com a latência de ~1 minuto, só B atende.** A e C ficam de fora pelo atraso.

**O custo de B:**
- o risco de termos é direto seu;
- você mantém o parser de HTML;
- você lida com bloqueio de IP.

As mitigações estão na seção 4: uma requisição por busca por ciclo, jitter, breaker com espera
crescente e aviso quando a fonte for bloqueada.

**Se B ficar bloqueado por muito tempo,** A pode entrar como adaptador reserva atrás da mesma
interface. Nesse período o atraso volta para ~1h.

**Regra fixa, qualquer que seja a fonte:** nunca fazer login nem reaproveitar cookies do
LinkedIn. Scraping logado, com contas falsas, foi o que levou hiQ, Proxycurl e ProAPIs ao
processo, e põe a sua conta em risco.

### D2. Linguagem

| | **Python (recomendado)** | TypeScript strict | Java |
|---|---|---|---|
| Runtime | 3.13+ (igual ao whatsapp-automation) | Node 24 LTS | Java 25 |
| HTTP | httpx 0.28.1, async | `fetch` nativo + `AbortSignal.timeout` | `java.net.http.HttpClient` |
| Retry/backoff | loop próprio em `utils/retry.py` (as regras por canal não cabem bem no tenacity) | cockatiel 4.0.0 | Resilience4j 2.4.0 |
| Circuit breaker | **copiar** `utils/circuit_breaker.py` (já existe nos seus 2 projetos) | cockatiel ou classe própria | Resilience4j |
| Validação de env | pydantic-settings 2.16.1 + `SecretStr` | zod 4.6.7 | `record` + checagem, ou Spring Boot 4.1.x `@Validated` |
| Resend | httpx direto | SDK `resend` 6.34.1 | `resend-java` 4.29.0 |
| Evolution | **copiar** `evolution_service.py` do whatsapp-automation | wrapper próprio (~30 linhas) | wrapper próprio |
| Tipagem | mypy strict + ruff (config já pronta) | `tsc --noEmit` strict | compilador |

Versões conferidas nos registros (npm, PyPI, Maven Central) em 2026-10-24.

**Por que Python.** Seis peças já existem e estão testadas nos seus repositórios: circuit breaker,
cliente Evolution, envio Resend, settings que falham sem ecoar valores, logging JSON e hooks de
`.claude/`. A config de ruff/mypy, o Makefile e o deploy por tag também já existem. Em TS ou Java,
tudo isso é reescrito.

**O custo de Python.** Os tipos só valem no CI (mypy), não em runtime. Fora das bordas validadas
pelo Pydantic, nada impede um tipo errado em produção. TS teria menos dependências e o mesmo nível
de checagem no CI.

**Se for TS:**
- "proibido JavaScript" vira regra de máquina: `allowJs: false`, `strict: true`,
  `noUncheckedIndexedAccess: true`;
- o CI falha se existir qualquer `.js`, `.mjs` ou `.cjs` fora de `dist/`.

**Se for Java:**
- usar Spring Boot 4.1.x, porque o suporte open source da linha 3.5 acabou em 2026-06-30
  ([spring generations](https://api.spring.io/projects/spring-boot/generations));
- ou Java puro, que é mais leve para um único loop.

### D3. Persistência do "já avisado"

| Opção | Sobrevive a restart | Falha típica | Custo |
|---|---|---|---|
| Só memória (default) | não | vagas publicadas durante um redeploy são perdidas | zero |
| `STATE_FILE` em volume do Railway (recomendado em produção) | sim | só 1 réplica; o redeploy tem uma pausa curta | 1 volume + 1 env |
| Redis | sim | mais um serviço para manter | 1 serviço + `REDIS_URL` |

**Recomendo** a opção só memória para desenvolvimento e `STATE_FILE` em produção. Redis fica de
fora: para um worker de uma réplica, não traz nada que o arquivo não traga.

O arquivo guarda **só `source:jobId` e a data**. Não guarda título, empresa nem recrutador
(LGPD, art. 7º: guardar o mínimo).

---

## 3. Como funciona

```
boot ─► valida env (falha listando só NOMES) ─► carrega STATE_FILE (se houver)
  │
  └─ estado vazio? ─► BASELINE: busca, marca tudo como visto, não envia nada
  │
no boot e depois a cada 2h: resumo com até 10 vagas dos últimos 3 dias, nunca mais antigas (f_TPR=r259200) ainda não enviadas,
  numa mensagem só; pega as vagas que entraram na busca depois da janela de 1h do ciclo
loop a cada POLL_INTERVAL_SECONDS ±20% (o próximo ciclo só começa quando o atual termina):
  1. para cada busca (no máximo 3):
       breaker da fonte aberto? → pula
       lê a janela da última hora (f_AL=true, f_TPR=r3600), até 3 páginas
       → valida cada item no schema (o inválido é descartado) → novas = id não visto
  2. novas? → uma mensagem por vaga; se vierem mais de 3 no mesmo ciclo, junta numa só (até 10 por mensagem)
  3. despacho:
       canal principal (NOTIFY_PRIMARY) ─ falhou ou breaker aberto ─► canal reserva
       WhatsApp: antes de enviar, consulta GET /instance/connectionState/{instance};
                 se não estiver "open", pula direto para o email [V]
       aceito (2xx) → marca os ids como vistos → grava STATE_FILE (escreve num temp e renomeia)
       os dois falharam → NÃO marca; loga NOTIFY_FAILED; o próximo ciclo tenta de novo
  4. remove do estado os ids com mais de 30 dias
SIGTERM ─► termina o envio em curso, grava o estado, fecha os clientes HTTP
```

**Por que juntar quando chegam várias de uma vez:**
- o Resend grátis permite 100 emails por dia [V];
- várias mensagens seguidas no WhatsApp parecem envio em massa, o que aumenta o risco de ban [I].

**Uma vaga, um aviso.** Para um ciclo comum, a deduplicação é por `jobId`. Os links da mensagem
são montados a partir do id (`https://www.linkedin.com/jobs/view/{id}`), nunca copiados da fonte.
Assim, um link adulterado vindo da fonte não chega ao seu WhatsApp.

**O que "entregue" significa.** O provedor aceitou a mensagem (2xx). Ler a mensagem já seria
confirmação de entrega, que exige webhook de status (fora do v1).

**Duplicata possível.** Se o principal der timeout e o reserva funcionar, você pode receber os
dois. É a troca do "pelo menos uma vez", e fica documentada no README.

**Ficar sabendo quando o worker para de funcionar.** Se a fonte falhar por 10 minutos seguidos, o
worker envia **um** aviso "fonte de vagas falhando desde HH:MM" pelos canais e não repete até ela
voltar. Sem isso, um bloqueio passaria em silêncio. Conta como falha também uma página 200 com HTML
mas sem card reconhecido (layout mudou ou página de verificação). Uma busca sem resultado volta sem
nenhuma tag (`<!DOCTYPE html><!---->`, observado em 2026-10-29) e continua sendo "0 vagas". Cada ciclo
grava uma linha `Cycle done: ...` no log, para distinguir "sem vagas" de "travado".

---

## 4. Resiliência

### Circuit breaker
Copiado do whatsapp-automation. É a mesma classe nos dois projetos, sem lock, porque roda num único
event loop.

| Dependência | Abre após | Fica aberto | Observação |
|---|---|---|---|
| Fonte de vagas | 3 falhas seguidas | 5 min, dobrando a cada nova abertura, até 60 min | 429 ou 999 (bloqueio no `jobs-guest` [I]) abre na hora; usa o `Retry-After` quando ele vier |
| Resend | 5 falhas | 60 s | |
| Evolution | 5 falhas | 30 s | mesmos valores do whatsapp-automation |

### Retry
- **Por canal:** até 3 tentativas, backoff exponencial com jitter. Só em erro de rede, 429 ou 5xx,
  respeitando o `Retry-After`.
- **Resend:** header `Idempotency-Key: job-<hash dos ids>`, guardado por 24h [V]. Repetir não
  duplica. O corpo precisa ser determinístico (sem horário), senão o Resend devolve 409 [V].
- **Evolution:** não tem chave de idempotência [V, subagente]. Repete só se a conexão falhou antes
  do envio, ou em 5xx. Nunca em timeout de leitura, que pode duplicar a mensagem.

### Rate limit
Aqui o rate limit é **de saída**: não existe API de entrada para limitar.

- **Fonte:** por busca e por ciclo, de 1 a 3 páginas de 10 vagas. Numa janela de 1h, quase sempre é 1.
  - `POLL_INTERVAL_SECONDS` com default 900 (15 min) e mínimo 60, validado no boot.
  - Jitter de ±20% no intervalo, para não bater sempre no mesmo segundo [I].
  - No máximo 3 buscas, com as requisições espaçadas dentro do ciclo.
  - Sem login, sem cookies, de um único IP.
  - Com 3 buscas a cada 2 min, são ~2.160 requisições por dia.
- **Resend:** 10 req/s e 100 por dia no plano grátis [V].
- **WhatsApp:** no máximo 6 mensagens por hora [I, margem anti-ban]. O que passar disso vai para o
  email ou entra na mensagem do próximo ciclo.

---

## 5. Segurança e vazamento de credencial

### O que de fato se aplica a este worker
1. **Segredos só por env.** Railway Variables em produção. `.env` só local e no `.gitignore`.
   `.env.example` com os nomes, sem valores.
2. **Falhar no boot sem ecoar valores.**
   - `SecretStr` nas chaves e `hide_input_in_errors=True`.
   - Repetir o padrão de `whatsapp-automation/app/config/settings.py`: só os nomes dos campos
     e `raise ... from None`.
3. **Chave nunca na URL.** URLs aparecem em mensagens de exceção e em log.
   - Resend: `Authorization: Bearer`.
   - Evolution: header `apikey`.
   - Apify: header `Authorization`, nunca `?token=`.
4. **Log limpo.**
   - Nunca logar headers, request ou corpo.
   - Mascarar email e telefone (`h***@gmail.com`, `5511*****9999`).
   - Logger do `httpx` em WARNING, porque em INFO ele loga a URL de cada request.
5. **Menor privilégio.**
   - Chave do Resend com permissão "Sending access" restrita ao domínio [I].
   - Na Evolution, o **token da instância**, não a chave global. O header aceita os dois [V].
6. **TLS de saída.** O boot rejeita `EVOLUTION_API_URL` sem `https://`. Exceção: a rede privada
   do Railway (`*.railway.internal`).
7. **Dado de fonte é dado não confiável** (OWASP API10:2023).
   - Schema Pydantic com limite de tamanho.
   - `html.escape` em título e empresa no email.
   - Link montado pelo id (seção 3).
8. **Varredura de segredos.**
   - gitleaks v8.30.1 no pre-commit e no CI.
   - Lockfile com versões fixas (`uv pip compile`, como no whatsapp-automation).
9. **Guardrails de agente.** `.claude/settings.json` (deny de `.env`,
   git que escreve) e `hooks/guard-bash.py`.
10. **WhatsApp só fala com um destino.** O único destino possível é `WHATSAPP_TO`, vindo do env.
    O worker não lê contatos e não responde mensagens. É a mesma garantia que você pediu no
    whatsapp-automation.

### O que NÃO se aplica (sem rota HTTP de entrada) e quando volta

| Item | No v1 | Se surgir... | Padrão a copiar |
|---|---|---|---|
| CORS | não existe | painel web → allowlist exata, sem `*` com credentials | — |
| Cookies | não existe | painel → httpOnly, Secure, SameSite=Lax + CSRF | — |
| Bearer de entrada | não existe | endpoint admin → `hmac.compare_digest` | `whatsapp-automation/app/utils/dependencies.py` |
| Rate limit de entrada | não existe | qualquer rota → por IP real, com `TRUSTED_PROXY_COUNT` | — |
| Ticket de WebSocket | não existe | painel ao vivo → ticket curto e assinado | — |

Um `/health` não é necessário: no Railway, o worker roda sem porta e o restart policy cuida de
quedas.

---

## 6. Variáveis de ambiente

**Regras:**
- Um canal **completo** é obrigatório.
- Canal pela metade dá erro com o nome do que falta.
- Os dois canais são recomendados, porque sem o segundo não há reserva, só a nova tentativa no
  próximo ciclo.

| Variável | Obrigatória | Default | Para quê | Como obter |
|---|---|---|---|---|
| `JOB_KEYWORDS` | sim | — | até 3 buscas, separadas por vírgula (`python, typescript`) | você define |
| `JOB_LOCATION` | sim | — | local da busca, **em inglês** (`Brazil`, `São Paulo`) | você define (`Brasil` é ignorado, seção 1.4) |
| `APIFY_TOKEN` | só se A entrar como reserva | — | autentica na Apify | console.apify.com → Settings → API & Integrations [I] |
| `RESEND_API_KEY` | canal email | — | envia o email | resend.com → API Keys → Create → "Sending access" |
| `EMAIL_TO` | canal email | — | quem recebe | sem domínio próprio, **tem que ser o email da sua conta Resend** [V] |
| `EMAIL_FROM` | não | `onboarding@resend.dev` | remetente | só troque depois de verificar um domínio em Resend → Domains (registros DNS) [V] |
| `EVOLUTION_API_URL` | canal WhatsApp | — | URL da Evolution | a mesma Evolution que já roda no Railway para o whatsapp-automation |
| `EVOLUTION_INSTANCE` | canal WhatsApp | — | instância que envia | crie uma **nova** (ex.: `job-alerts`) com número dedicado, não o da loja |
| `EVOLUTION_API_KEY` | canal WhatsApp | — | token da instância | Evolution Manager, na instância (local exato: conferir na v2.3.7) |
| `WHATSAPP_TO` | canal WhatsApp | — | seu número (`5511999999999`) ou grupo (`...@g.us`) | grupo: `GET /group/fetchAllGroups/{instance}` (README do whatsapp-automation) |
| `NOTIFY_PRIMARY` | não | `whatsapp` | canal tentado primeiro (só importa com os 2) | — |
| `POLL_INTERVAL_SECONDS` | não | `900` | intervalo entre ciclos; mínimo 60 | — |
| `STATE_FILE` | não | vazio = só memória | ex.: `/data/seen.json` num volume | Railway → serviço → Volumes → montar em `/data` |
| `LOG_LEVEL` | não | `INFO` | — | — |

**Mínimo para funcionar:** 4 variáveis (`JOB_KEYWORDS`, `JOB_LOCATION`, `RESEND_API_KEY`,
`EMAIL_TO`), com a fonte B e só email.

**Configuração recomendada:** 9 variáveis, com a fonte B, os dois canais e `STATE_FILE`.

**Por que o número do WhatsApp é dedicado.** Enviar mensagem que o bot inicia viola os termos do
WhatsApp, mesmo para um único destino ([terms](https://www.whatsapp.com/legal/terms-of-service)).
O risco cai muito quando é para o seu próprio número, sem contatos frios [I], mas não chega a zero.
Mande um "oi" do seu número para o bot antes, para a conversa existir dos dois lados.

**Se o WhatsApp tiver que ser confiável:**
- trocar para a Cloud API oficial (`integration: "WHATSAPP-BUSINESS"` na Evolution) [V];
- isso exige um template utility aprovado, porque fora da janela de 24h só template é enviado [V];
- custo de ~US$ 0,0068 por mensagem no Brasil [V].

---

## 7. Organização do código

O código segue uma arquitetura em camadas (rotas, serviços, repositórios), adaptada para um worker sem HTTP.

| Camada padrão | linkedin-api | Nota |
|---|---|---|
| `routes/` | — | sem HTTP de entrada; `main.py` roda o loop |
| `services/` | `services/watcher.py`, `services/dispatcher.py` | ciclo e principal/reserva |
| `repositories/` | `state/seen_store.py` | única camada que lê e grava estado |
| `schemas/` | `schemas/job.py` | valida o dado externo na borda |
| `models/` (ORM) | — | sem banco |
| `errors/` | `errors.py` | `SourceUnavailableError`, `RetryableError`, `ChannelUnavailableError`, `ChannelRejectedError`; internas, sem status HTTP |
| `config/` | `config/settings.py`, `config/logging_config.py` | copiar o padrão |
| `utils/` | `utils/circuit_breaker.py`, `utils/mask.py` | breaker copiado |
| `services/messenger.py` (whatsapp-automation) | `channels/base.py` + `email.py` + `whatsapp.py` | porta `Notifier` |
| — | `sources/base.py` + adaptador escolhido em D1 | porta `JobSource` |

```
linkedin-api/
  app/
    main.py                  # boot, loop, SIGTERM
    config/settings.py       # env + regra "um canal completo"
    config/logging_config.py # JSON em produção, httpx em WARNING
    errors.py
    schemas/job.py
    sources/base.py          # Protocol JobSource
    sources/<fonte>.py       # só o adaptador escolhido em D1
    channels/base.py         # Protocol Notifier
    channels/email.py        # Resend via httpx + Idempotency-Key
    channels/whatsapp.py     # Evolution: connectionState + sendText
    services/watcher.py      # ciclo
    services/dispatcher.py   # principal → reserva
    services/messages.py     # texto do aviso (determinístico, para a idempotência)
    state/seen_store.py      # memória + STATE_FILE atômico
    utils/circuit_breaker.py
    utils/retry.py
    utils/mask.py
  tests/
  .claude/settings.json + hooks/guard-bash.py
  .github/workflows/ci.yml     # ruff, mypy, pytest, gitleaks
  .github/workflows/deploy.yml # deploy no Railway por tag v*
  .env.example  .gitignore  Dockerfile  Makefile  pyproject.toml  requirements.in/.txt
  README.md (inglês)  AGENTS.md (inglês)
```

**Regras de código:**
- sem `Any`, com mypy strict;
- tudo async;
- estado em memória sem lock, porque é um único event loop;
- sem código morto;
- validação na borda;
- sem overengineering;
- `make lint` antes de entregar.

Os comentários ficam em inglês e enxutos.

---

## 8. Fases

1. **Esqueleto.**
   - Settings e logging.
   - `.claude/`, gitleaks e ruff/mypy.
   - `.env.example`.
2. **Canais.**
   - Email e WhatsApp, cada um com breaker e retry.
   - Dispatcher com reserva.
   - Testes com `httpx.MockTransport`: só o HTTP externo é simulado.
3. **Fonte e ciclo.**
   - Adaptador escolhido em D1 e schema.
   - Baseline, dedupe e `STATE_FILE`.
   - Alerta de "fonte falhando".
4. **Deploy.**
   - Railway como worker, com volume.
   - `deploy.yml` por tag.
   - README em inglês com o passo a passo de cada env.

## 9. Critérios de aceite

- **Boot:**
  - [ ] Boot sem canal completo encerra com erro listando só nomes de variáveis.
  - [ ] Primeiro ciclo com estado vazio não envia nada (baseline).
- **Envio:**
  - [ ] Uma vaga nova com Easy Apply chega pelo canal principal em até 1 ciclo (~2 min) depois
        de aparecer na busca.
  - [ ] Com o principal fora (breaker aberto, ou WhatsApp sem estar `open`), o aviso chega pelo
        reserva no mesmo ciclo.
  - [ ] Com os dois fora, nada é marcado, o próximo ciclo reenvia e cada ciclo loga um
        `NOTIFY_FAILED`.
- **Duplicidade:**
  - [ ] Com `STATE_FILE`, um restart não reenvia vagas já avisadas.
- **Segredos e dados:**
  - [ ] Nenhum valor de segredo aparece em log. Teste: rodar com chaves conhecidas e procurar por
        elas na saída.
  - [ ] O estado gravado contém só ids e datas.
- **Robustez:**
  - [ ] A fonte falhando por 10 min seguidos gera um único aviso.

## 10. Fora do escopo do v1

- **Login ou cookies do LinkedIn.**
- **Listar todas as vagas, API REST, painel, vários usuários e banco de dados.**
- **Confirmação de leitura ou entrega**, via webhooks de status.

---

## Mapa da documentação oficial do LinkedIn (navegado)

- [developer.linkedin.com](https://developer.linkedin.com/) →
  [product-catalog](https://developer.linkedin.com/product-catalog). As docs ficam em
  `learn.microsoft.com/en-us/linkedin/`.
- **Talent**
  ([toc](https://learn.microsoft.com/en-us/linkedin/talent/toc.json?view=li-lts-2026-04))
  - Apply Connect: create jobs, OnsiteApplyConfiguration, receive applications, status sync,
    certification.
  - Apply with LinkedIn: fechado a novos parceiros.
  - Job Posting: XML Feed Guide; API Guide (create/update/close, status, notificações de status e
    resync).
  - Recruiter System Connect e CRM Connect.
- **Shared**
  - Autenticação:
    - 3-legged: token de 60 dias.
    - 2-legged: token de 30 min, sem acesso por padrão.
    - Refresh de 365 dias, só para parceiros.
  - Webhooks:
    - validação por `challengeCode` + HMAC-SHA256 com o client secret;
    - assinatura no header `X-LI-Signature`.
  - Rate limits:
    - por app e por membro, com reset à meia-noite UTC e 429 quando passa;
    - os valores não são publicados.
  - Versionamento: header `LinkedIn-Version: YYYYMM`; versão encerrada responde 426.
- **Termos**
  - [API Terms](https://www.linkedin.com/legal/l/api-terms-of-use)
  - [User Agreement](https://www.linkedin.com/legal/user-agreement)
  - [Crawling Terms](https://www.linkedin.com/legal/crawling-terms)
  - [Job Posting API Terms](https://www.linkedin.com/legal/l/job-posting-api-terms)
