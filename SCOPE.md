# linkedin-api — escopo

O que o projeto é, o que fica de fora e como uma mudança entra. As regras de negócio estão no
[CONTEXT.md](CONTEXT.md); as decisões difíceis de desfazer, com alternativas e custos, em [docs/adr/](docs/adr/);
como configurar e rodar, no [README.md](README.md).

## O que é

Um worker de um processo só que, para as buscas configuradas, detecta vaga nova em fontes públicas, dá a cada uma
uma nota contra o currículo de quem usa e avisa as que valem a candidatura, com um currículo ajustado à vaga.
Quem usa lê o aviso e se candidata por conta própria.

## Dentro do escopo

- **Ler vagas** de fontes públicas, sem chave, login nem cookie: LinkedIn (busca `jobs-guest`), Gupy, Remotar,
  Himalayas e Workable (ADR 0002, ADR 0007).
- **Dar nota** de 0 a 100 a cada vaga nova com uma IA, e descartar as que não valem o aviso (ADR 0005).
- **Ajustar o currículo** à vaga: PDF para as fontes que pedem arquivo, texto dos campos para a Gupy (ADR 0008).
- **Avisar** por email (Resend), WhatsApp (Evolution) ou Telegram (Bot API), com principal e reserva (ADR 0006).
- **Resumo periódico** de vagas de qualquer data e **comandos** no Telegram (`/resumo`, `/vagas`, `/status`).
- **Lembrar o que já foi tratado**, em memória ou num arquivo (ADR 0004).

## Fora do escopo

Cada item tem a condição em que ele voltaria a ser discutido.

| Fora | Por quê | Volta se |
|---|---|---|
| Candidatar-se a vagas | a candidatura é de quem usa; automatizar viola os termos das fontes | nunca |
| Login ou cookie em qualquer fonte | foi o que levou os casos de scraping do LinkedIn ao processo, e põe a conta de quem usa em risco | nunca |
| Porta de entrada: API REST, painel, webhook, WebSocket, `/health` | o worker não precisa receber nada, e cada porta é superfície de ataque (ADR 0009) | uma fonte passar a oferecer push, ou surgir painel ao vivo ou outro sistema que consuma os eventos |
| Banco de dados e vários usuários | um usuário, uma réplica, só ids e datas para guardar (ADR 0004) | houver mais de um usuário |
| Confirmação de leitura ou entrega | exige os webhooks de status do Resend ou da Evolution, que pedem uma porta | a entrega precisar ser auditada |
| WhatsApp pela Cloud API oficial | pede template aprovado e custa por mensagem | o WhatsApp precisar ser confiável |
| Deduplicar a mesma vaga entre fontes | descartado; a repetição é um limite aceito | a repetição virar ruído frequente |
| Comandos além de `/resumo`, `/vagas` e `/status` (pausar, botões, texto livre) | não pedidos | alguém pedir |
| Servidor MCP para consultar vagas | o `/vagas` já cobre a consulta, e um MCP remoto abriria uma porta | a consulta precisar sair do Telegram |

## Regras que toda mudança respeita

- **Sem custo fixo.** Fontes públicas, plano grátis da IA e dos canais. Algo pago só entra com um ADR.
- **Dados mínimos.** O estado guarda só `fonte:id` e a data; o bloco `contato` do currículo nunca vai para a IA;
  log sem segredo, sem corpo e com email e telefone mascarados (`AGENTS.md`, princípio 4).
- **Dado de fonte e de IA é dado não confiável.** Passa por schema com limite de tamanho antes de qualquer uso; o
  link é montado pelo id.
- **Um destino por canal.** O worker manda só para `EMAIL_TO`, `WHATSAPP_TO` e `TELEGRAM_CHAT_ID`, e não lê contatos.
- **Vaga não se perde.** Ela só vira vista depois que um canal aceitou o aviso (CONTEXT.md, BR-003).

## Limites aceitos

- O worker não controla quanto tempo a fonte leva para mostrar a vaga na busca; o aviso sai no ciclo seguinte.
- Ler a busca pública viola os termos do LinkedIn (User Agreement §8.2) e pode levar a bloqueio do IP: o risco é de
  quem roda o worker (ADR 0002). As outras fontes também são páginas ou JSON não oficiais e podem mudar sem aviso.
- Robô em número comum de WhatsApp viola os termos do WhatsApp; o risco cai, mas não some, com um número dedicado
  que fala só com o próprio dono.
- Um aviso pode chegar duas vezes (principal com timeout e reserva que funcionou, ou a mesma vaga em duas fontes).
- A nota varia alguns pontos entre execuções e entre modelos.

## Critérios de aceite

Valem para qualquer mudança: o que já está aqui continua verdadeiro depois dela.

- **Boot**
  - Sem um canal completo, ou sem `LLM_API_KEY` e currículo, o worker encerra com erro listando só nomes de variáveis.
  - O primeiro ciclo de cada busca não envia nada (baseline).
  - Nenhum resumo sai nas primeiras 6 horas depois do boot.
- **Nota e envio**
  - Vaga abaixo de 50, presencial ou encerrada não chega em canal nenhum.
  - Vaga 50+ chega com as duas notas e com o currículo ajustado (o texto da Gupy, numa vaga da Gupy).
  - Com o principal fora (breaker aberto, ou WhatsApp sem `open`), o aviso chega pela reserva no mesmo ciclo.
  - Com todos os canais fora, nada é marcado, e o próximo ciclo reenvia.
  - Com `STATE_FILE`, um restart não reenvia vaga já avisada.
- **Fontes**
  - Cada vaga mostra a fonte no fim da linha, e o link abre a vaga certa.
  - Todas as fontes são lidas em todo ciclo, ao mesmo tempo; com o LinkedIn cheio de vagas, o resumo e o `/vagas`
    ainda trazem as das outras.
  - Uma fonte falhando por 1 hora gera um aviso só dela, e as outras seguem avisando.
  - Na Gupy, só chegam vagas do `JOB_LOCATION` (ou sem local).
- **Telegram**
  - Com só `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, o worker sobe e o aviso chega.
  - Título com `<`, `&` ou `'` chega intacto; um aviso acima de 4096 caracteres chega inteiro, em várias mensagens.
  - Mensagem de outro chat não roda nada nem recebe resposta.
  - Com o robô bloqueado ou o token revogado, o aviso sai pela reserva.
- **Segredos e dados**
  - Nenhum valor de segredo aparece em log, nem o token do Telegram nos erros.
  - O estado gravado contém só ids e datas.

## Como manter estes documentos

- **Este arquivo** diz o que o projeto faz e não faz. Mudou o escopo (entrou uma fonte, um canal, um comando, saiu
  um item da tabela de fora): ele muda na mesma alteração.
- **Regra de negócio** (nota mínima, o que é descartado, o que vai na mensagem) vai para o [CONTEXT.md](CONTEXT.md),
  com o motivo e o arquivo que a aplica.
- **Decisão difícil de desfazer, com alternativas reais,** vira um ADR novo em [docs/adr/](docs/adr/) (critérios no
  ADR 0001). ADR aceito não é editado: um novo o substitui.
- **Sem datas e sem histórico.** Os documentos descrevem como o projeto é agora; o histórico fica no Git.
- **Sem nomes de pessoas.** O repositório é público: as regras falam do projeto e de "quem usa".
- **Fatos de fora com a origem.** O que foi visto numa fonte leva o link ou o nome do campo; o que é inferido diz
  que é.
