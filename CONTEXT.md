# linkedin-api — regras de negócio

Um worker que lê buscas públicas de vagas (LinkedIn, Gupy, Remotar, Himalayas e Workable), dá a cada vaga nova uma
nota contra o currículo de quem usa e avisa por email, WhatsApp ou Telegram as que valem a candidatura, com um
currículo ajustado à vaga. Ele nunca se candidata. O que fica dentro e fora do projeto está no
[SCOPE.md](SCOPE.md); as decisões difíceis de desfazer, em [docs/adr/](docs/adr/).

## Business rules

Cada regra diz o que vale, por quê e onde o código aplica. Mudou uma regra: este arquivo muda na mesma alteração.

### Nota e envio

### BR-001: Só sai vaga com nota 50 ou mais, fora presencial

- **Rule:** uma vaga com nota abaixo de 50, presencial ou encerrada é marcada como vista e nunca sai, em nenhum
  canal. A nota que decide é a do currículo base.
- **Why:** o aviso tem que valer a candidatura; marcar a descartada evita gastar a cota da IA com ela de novo.
- **Enforced in:** `MIN_SCORE` em `app/services/tailor.py`; `Watcher._assess` em `app/services/watcher.py`.
- **Exceptions:** o `/vagas` lista qualquer nota (BR-015).

### BR-002: Vaga sem nota espera

- **Rule:** com a IA fora, a cota esgotada ou a página da vaga indisponível, a vaga não sai nem é marcada: o próximo
  ciclo ou resumo dá a nota. Uma página que falha 3 vezes sem bloqueio (quebrada) é desistida e marcada. Quando só
  o currículo ajustado falha, a vaga sai com "Currículo ajustado indisponível".
- **Why:** mandar vaga sem nota volta a encher o canal de ruído; uma página quebrada não pode ocupar todo resumo.
- **Enforced in:** `Watcher._assess` e `_MAX_PAGE_FAILURES` em `app/services/watcher.py`; `RESUME_DOWN` em
  `app/services/messages.py`.
- **Exceptions:** none

### BR-003: Vaga é marcada como vista só depois que um canal aceitou

- **Rule:** os ids viram vistos só quando um canal confirmou o envio (2xx). Com todos os canais fora, nada é marcado,
  e o próximo ciclo tenta de novo.
- **Why:** perder uma vaga é pior que recebê-la duas vezes ("pelo menos uma vez").
- **Enforced in:** `Watcher._notify` e `Watcher._digest` em `app/services/watcher.py`.
- **Exceptions:** vaga descartada (BR-001) e página desistida (BR-002) são marcadas sem envio.

### BR-004: A primeira leitura de uma busca é baseline

- **Rule:** na primeira resposta de cada busca (`fonte|busca|local`), tudo o que veio é marcado como visto e nada é
  enviado. Mudar a busca ou o local cria uma baseline nova.
- **Why:** o que já estava publicado não é vaga nova, e daria nota a uma enxurrada de vagas de uma vez.
- **Enforced in:** `Watcher.cycle` em `app/services/watcher.py`; `SeenStore` em `app/state/seen_store.py`.
- **Exceptions:** o resumo (BR-013) lê vagas de qualquer data ainda não vistas.

### BR-005: Limites da nota

- **Rule:** a IA dá a nota com estas regras, e o código aplica as que o modelo não seguiu sozinho:
  - vaga que não cita nenhuma tecnologia do currículo vai no máximo a 30 (código);
  - inglês obrigatório acima do nível do currículo limita a 50 (código): a vaga que bate em todo o resto ainda
    sai, e qualquer outra lacuna a derruba;
  - vaga sênior, lead, staff ou especialista vai no máximo a 40; uma faixa com um nível buscado ("pleno a sênior",
    "Mid-Senior level") não é sênior;
  - vaga que não é de desenvolvimento (vendas, suporte, operações) vai no máximo a 10;
  - versão não é falta: "Java 17" vira "Java" antes de ir para a IA ("Java 5 anos" e "S3" ficam);
  - "ou" é alternativa e "e" é soma; tecnologia parecida e item desejável baixam bem menos que um requisito.
- **Why:** sem essas regras a IA dava 100 a uma vaga de vendas de uma frase, contava versão como falta, aplicava o
  limite de sênior só às vezes e deixava passar vaga com inglês fluente para um currículo A2.
- **Enforced in:** `NO_EVIDENCE_MAX_SCORE`, `ENGLISH_MAX_SCORE`, `without_versions` e o prompt em
  `app/services/assessor.py`.
- **Exceptions:** none

### BR-006: Duas notas na mensagem

- **Rule:** depois do ajuste, o mesmo modelo da nota dá uma segunda nota ao que vai no anexo, e a mensagem mostra as
  duas: a segunda com a diferença para a base, o motivo e o que ainda falta. Só a nota base decide o envio. Com a IA
  fora nessa hora, a vaga sai sem a segunda nota.
- **Why:** quem recebe precisa saber quanto vale o arquivo enviado e o que melhorar. O ajuste só escolhe e reordena o
  que o currículo já tem: o que ainda falta depois dele falta no próprio currículo, e uma diferença grande entre as
  duas notas é ruído da IA, não uma melhora.
- **Enforced in:** `JobAssessor.score_resume` em `app/services/assessor.py`; `ResumeTailor.tailor` em
  `app/services/tailor.py`; `_resume_lines` em `app/services/messages.py`.
- **Exceptions:** none

### Currículo ajustado

### BR-007: A IA escolhe, o código monta

- **Rule:** a IA só devolve ids de destaques, um projeto, habilidades e um resumo reescrito. Empresa, cargo, datas e
  formação vêm sempre do YAML; id que não existe é ignorado; habilidade fora do currículo cai;
  `habilidades_sob_demanda` só entra se a vaga citar; projeto `fixo: true` entra sempre, e a IA escolhe mais um.
- **Why:** o currículo nunca pode afirmar algo que a pessoa não fez.
- **Enforced in:** `build_tailored` em `app/services/tailor.py`; `TailoredResume` em `app/schemas/resume.py`.
- **Exceptions:** na Gupy, todos os projetos entram (BR-009).

### BR-008: Resumo ajustado com falha segura

- **Rule:** o resumo reescrito volta ao original quando cita um termo técnico que o currículo não tem, um nível
  (júnior, pleno, sênior) ou uma palavra do título da vaga que o currículo não tem. Ele descreve a pessoa, nunca as
  tarefas, o produto ou a área da vaga.
- **Why:** "plataforma de trading" num currículo sem trading parece mentira para quem lê.
- **Enforced in:** `safe_summary` e o prompt em `app/services/tailor.py`.
- **Exceptions:** palavra comum vinda da descrição não é barrada: dependeria de comparar texto livre.

### BR-009: Na Gupy, o texto dos campos em vez do PDF

- **Rule:** vaga da Gupy leva só `Gupy-<empresa>.txt`: cada bloco é um item de Experiência (empresa, cargo, início,
  fim ou atual, descrição), todo projeto entra como experiência com rótulo honesto (freelance com o cliente,
  projeto pessoal como "Projeto próprio: <nome>"), e no fim vão as 30 habilidades mais ligadas à vaga. Projeto sem
  `inicio` pede para preencher em vez de inventar a data.
- **Why:** a IA da Gupy só lê Experiência e Habilidades, não lê o PDF e não tem campo de projetos (ADR 0008).
- **Enforced in:** `ResumeTailor.tailor` em `app/services/tailor.py`; `gupy_resume` e `gupy_text` em
  `app/services/gupy_text.py`.
- **Exceptions:** none

### BR-010: O contato nunca vai para a IA

- **Rule:** o bloco `contato` do currículo só é lido para imprimir o PDF; nenhum prompt o inclui.
- **Why:** dado pessoal que a nota e o ajuste não precisam (LGPD, minimização).
- **Enforced in:** `Resume` e `Contact` separados em `app/schemas/resume.py`.
- **Exceptions:** none

### Fontes e buscas

### BR-011: A mesma busca em todas as fontes

- **Rule:** `JOB_KEYWORDS` tem até 3 buscas na sintaxe do LinkedIn. Nas fontes que não entendem OR, cada alternativa
  vira uma requisição; os termos NOT são aplicados no título pelo worker, em todas as fontes. Toda fonte é lida em
  todo ciclo, ao mesmo tempo; dentro de uma fonte, as buscas seguem uma por vez, espaçadas.
- **Why:** uma busca só para configurar, e uma fonte lenta ou bloqueada não segura as outras.
- **Enforced in:** `MAX_SEARCHES` em `app/config/settings.py`; `app/utils/search_query.py`;
  `TermSearchSource.fetch_recent` em `app/sources/base.py`; `Watcher._read_sources` em `app/services/watcher.py`.
- **Exceptions:** none

### BR-012: O local da busca

- **Rule:** `JOB_LOCATION` vai em inglês, como o LinkedIn escreve (`Brazil`, `São Paulo`). Na Gupy, fica a vaga cuja
  cidade, estado ou país (Brasil quando o estado vem preenchido) é a primeira parte do `JOB_LOCATION`, sem acento nem
  maiúscula; vaga sem local nenhum passa. Remotar, Himalayas e Workable ficam só com o Brasil.
- **Why:** o LinkedIn ignora `Brasil` e volta vagas dos EUA; a Gupy também tem vagas fora do Brasil (Lisboa, Coimbra)
  e não informa o país: vaga do Brasil vem com o estado, a de fora com a cidade e o estado vazio.
- **Enforced in:** `_Item.in_location` em `app/sources/gupy_portal.py`; `country=BR` em `app/sources/himalayas.py`;
  `COUNTRY` em `app/sources/workable.py`.
- **Exceptions:** um país que não é o Brasil só bate na Gupy pela cidade (`Lisbon` sim, `Portugal` não); vaga remota
  sem local, mas só para quem mora fora, passa (o país só aparece no texto, e a nota lê o texto).

### Resumo e comandos

### BR-013: Resumo a cada 6 horas, em rodadas

- **Rule:** a cada 6 horas, nunca no boot, o resumo lê vagas de qualquer data ainda não vistas, dá nota de 10 em 10 e
  para na primeira rodada com alguma 50+, mandando as daquela rodada numa mensagem. No máximo 5 rodadas; uma rodada
  com vaga sem nota encerra o resumo. Nenhuma 50+: nenhuma mensagem. Se uma fonte não respondeu, o resumo continua
  pendente e roda de novo no próximo ciclo.
- **Why:** lista o que bate com o currículo, seja a vaga de um dia ou de um ano, sem passar da cota grátis da IA:
  um resumo gasta até 50 notas.
- **Enforced in:** `DIGEST_EVERY`, `MAX_DIGEST_ROUNDS` e `Watcher._digest` em `app/services/watcher.py`.
- **Exceptions:** cada deploy zera o relógio de 6 horas.

### BR-014: Comandos só do chat configurado

- **Rule:** o robô do Telegram atende `/resumo`, `/vagas` e `/status` (e `/start`) só do `TELEGRAM_CHAT_ID`; outro chat
  é ignorado sem resposta. `/resumo` e `/vagas` rodam no máximo uma vez a cada 30 minutos, cada um com o próprio
  relógio, e o `/resumo` não muda o horário do resumo automático. No boot, os comandos que chegaram com o robô
  desligado são pulados. Num grupo, qualquer membro pode mandar comando.
- **Why:** cada comando gasta até 50 notas e lê todas as fontes; um comando antigo não pode rodar depois de um deploy.
- **Enforced in:** `MANUAL_COOLDOWN` e `Commands` em `app/services/commands.py`; `TelegramInbox` em
  `app/channels/telegram.py`.
- **Exceptions:** none

### BR-015: `/vagas` só mostra

- **Rule:** `/vagas` lista as primeiras 20 vagas de qualquer data, vistas ou não, com qualquer nota (inclusive
  presencial e abaixo de 50), da maior para a menor, sem currículo e sem cópia por email. Só marca como vista a vaga
  encerrada, que fica de fora.
- **Why:** é uma consulta: não pode tirar do resumo uma vaga 50+, que depois sai com o currículo.
- **Enforced in:** `LIST_SIZE` e `Watcher.list_now` em `app/services/watcher.py`.
- **Exceptions:** com a IA ou a fonte fora, a vaga aparece sem nota.

### Canais

### BR-016: Canal principal, reserva e cópia por email

- **Rule:** o aviso vai pelo `NOTIFY_PRIMARY`; se ele falhar ou estiver com o breaker aberto, pelos outros, na ordem
  email, WhatsApp, Telegram. O email, se configurado, recebe sempre uma cópia do que outro canal entregou; falha na
  cópia só vai para o log.
- **Why:** um canal fora não pode calar o aviso, e o email guarda o histórico com os anexos.
- **Enforced in:** `Dispatcher` em `app/services/dispatcher.py`; ordem dos canais em `app/config/settings.py`.
- **Exceptions:** a resposta ao `/vagas` não tem cópia. Se o principal der timeout e a reserva funcionar, o aviso pode
  chegar duas vezes.

### BR-017: Um destino só por canal

- **Rule:** o WhatsApp manda só para `WHATSAPP_TO`, confere `connectionState` antes e manda no máximo 6 mensagens por
  hora; o Telegram manda só para o `TELEGRAM_CHAT_ID` numérico. Nada lê contatos nem responde mensagens.
- **Why:** robô em número comum viola os termos do WhatsApp; o risco cai muito quando ele fala só com o próprio dono,
  sem contato frio.
- **Enforced in:** `HOURLY_CAP` em `app/channels/whatsapp.py`; validação de `WHATSAPP_TO` e `TELEGRAM_CHAT_ID` em
  `app/config/settings.py`.
- **Exceptions:** none

### BR-018: Várias vagas de uma vez vão juntas

- **Rule:** até 3 vagas novas no mesmo ciclo saem uma por mensagem; mais que isso, juntas, até 10 por mensagem.
- **Why:** o Resend grátis permite 100 emails por dia, e várias mensagens seguidas no WhatsApp parecem envio em massa.
- **Enforced in:** `SINGLE_LIMIT`, `MAX_PER_MESSAGE` e `batches` em `app/services/messages.py`.
- **Exceptions:** none

### BR-019: Aviso inteiro ou nada

- **Rule:** se qualquer parte de um aviso falhar no Telegram (um bloco de texto ou um arquivo), o aviso inteiro conta
  como falha e vai pelo próximo canal. A mensagem não tem horário no texto, e o PDF tem data de criação fixa.
- **Why:** não perder vaga nem currículo; e o mesmo aviso precisa dar a mesma chave de idempotência no Resend (corpo
  diferente na mesma chave dá 409).
- **Enforced in:** `TelegramNotifier` em `app/channels/telegram.py`; `job_message` em `app/services/messages.py`;
  `app/services/resume_pdf.py`.
- **Exceptions:** parte do aviso pode chegar repetida no Telegram, que não tem chave de idempotência.

### BR-020: Aviso de fonte caída

- **Rule:** uma fonte que falha por 1 hora seguida gera **um** aviso com o nome dela, e não repete até ela voltar. Cada
  fonte tem o próprio relógio. Página 200 sem vaga reconhecida conta como falha; busca sem resultado é "0 vagas".
- **Why:** um bloqueio não pode passar em silêncio, mas o 429 do IP do Railway costuma sumir em 15-30 minutos.
- **Enforced in:** `SOURCE_DOWN_ALERT_AFTER` e `Watcher._track_source_health` em `app/services/watcher.py`.
- **Exceptions:** none

## Limites de chamadas

Números que o código segue; mudar um deles é mudar uma regra.

| Dependência | Breaker abre após | Fica aberto | Observação |
|---|---|---|---|
| LinkedIn | 3 falhas | 5 min, dobrando até 60 min | 429 ou 999 abre na hora; respeita `Retry-After` |
| Gupy, Remotar, Himalayas, Workable | 3 falhas | 5 min, dobrando até 60 min | 403 ou 429 abre na hora; 2 s entre alternativas |
| IA | 3 falhas | 60 s, dobrando até 60 min | 429 com `Retry-After` longo (cota) abre pelo tempo informado |
| Resend | 5 falhas | 60 s | `Idempotency-Key` com o hash dos ids, guardada 24 h |
| Evolution | 5 falhas | 30 s | sem idempotência: repete só se a conexão falhou antes do envio, ou em 5xx |
| Telegram | 5 falhas | 30 s | 429 com `retry_after` até 30 s espera e repete; acima disso, a reserva assume |

- Ciclo a cada `POLL_INTERVAL_SECONDS` (padrão 1800, mínimo 60), com ±20% de variação.
- Canais: até 2 novas tentativas, com espera crescente, só em erro de rede, 429 ou 5xx; nunca em timeout de leitura
  da Evolution, que pode duplicar a mensagem.
- Descrição cortada em 6.000 caracteres antes da IA; notas guardadas em memória para até 500 vagas e currículos
  ajustados para até 100, para uma nova tentativa não gastar cota.

## Language

**Vaga**:
Um anúncio de emprego de uma fonte, identificado por `fonte:id`.
_Avoid_: job (no texto em português), anúncio

**Fonte**:
Um site de onde as vagas são lidas: LinkedIn, Gupy, Remotar, Himalayas ou Workable.
_Avoid_: provedor (é o termo dos canais e da IA)

**Busca**:
Um dos termos de `JOB_KEYWORDS`, com OR e NOT.
_Avoid_: filtro, query

**Ciclo**:
Uma leitura da última hora em todas as fontes, a cada `POLL_INTERVAL_SECONDS`.
_Avoid_: rodada (é a parte de um resumo)

**Resumo**:
A leitura de vagas de qualquer data a cada 6 horas (ou pelo `/resumo`), em rodadas de 10.
_Avoid_: digest (no texto em português), relatório

**Baseline**:
A primeira leitura de uma busca, que marca tudo como visto sem enviar.

**Vista**:
Vaga já tratada (enviada ou descartada); não volta em ciclo nem em resumo.
_Avoid_: lida, enviada

**Nota**:
A nota de 0 a 100 da vaga contra o currículo base; decide o envio.

**Nota do currículo ajustado**:
A nota da mesma vaga contra o que vai no anexo; só informa.

**Currículo base**:
O YAML de `RESUME_FILE` ou `RESUME_YAML`, sem o bloco `contato` quando vai para a IA.

**Currículo ajustado**:
O currículo base com destaques, projetos e habilidades escolhidos e ordenados para uma vaga; vai como PDF.

**Texto da Gupy**:
O currículo ajustado no formato dos campos de Experiência e Habilidades da Gupy; vai no lugar do PDF.

**Canal**:
Um meio de aviso: email (Resend), WhatsApp (Evolution) ou Telegram (Bot API).

## Relationships

- Uma **busca** é lida em toda **fonte**; cada **vaga** vem de uma fonte só.
- Uma **vaga** recebe uma **nota**; com nota 50+, ganha um **currículo ajustado** (ou **texto da Gupy**) e a **nota do
  currículo ajustado**.
- Um aviso sai por um **canal** e só então suas vagas viram **vistas**.

## Flagged ambiguities

- A mesma vaga publicada em duas fontes chega duas vezes: deduplicar por título e empresa foi descartado.
