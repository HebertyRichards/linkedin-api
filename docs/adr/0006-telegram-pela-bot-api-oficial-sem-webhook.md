---
status: accepted
type: software
---

# 6. Telegram pela Bot API oficial, sem webhook

## Context

O WhatsApp pela Evolution usa um número comum: robô assim viola os termos do WhatsApp, cai quando o QR Code
expira, não manda PDF e tem teto de 6 mensagens por hora. Os comandos (`/resumo`, `/vagas`, `/status`) precisam
de um jeito de receber mensagens, e o projeto não abre nenhuma porta.

## Decision

Um canal Telegram opcional pela [Bot API](https://core.telegram.org/bots/api) oficial: texto em HTML e o
currículo como arquivo (`sendDocument`). Os comandos são lidos por long polling (`getUpdates`), sem webhook. O
robô só fala com o `TELEGRAM_CHAT_ID` (numérico) e ignora qualquer outro chat sem responder.

## Alternatives

- **Só WhatsApp:** sem arquivo, sem comandos e com risco de banimento.
- **Webhook do Telegram:** abriria a primeira porta de entrada do projeto (ver ADR 0009).
- **`sendMediaGroup`:** pede no mínimo 2 arquivos; seriam dois caminhos de código para o mesmo resultado.

## Trade-offs

- **We gain:** canal oficial e gratuito, com arquivo e comandos, sem porta aberta.
- **We pay:** o token vai na URL da requisição, então nenhuma URL pode ir para o log; duas instâncias lendo o mesmo
  robô (sobreposição no deploy) recebem 409 por um momento.
- **The rejected option would cost:** risco de ban e nenhum comando (só WhatsApp), ou uma porta pública (webhook).

## Consequences

- `utils/mask.redact` mascara o formato do token; erros usam o nome da exceção, nunca `str(e)`. Token vazado:
  `/revoke` no @BotFather.
- Nome de canal (`@canal`) é recusado no boot: o aviso tem dados da busca e vai só para quem configurou o worker.
- O texto vindo de fora passa por `html.escape(..., quote=False)`: o `&#x27;` do escape padrão dá erro 400.
- Rever se o Telegram mudar os termos para robôs, ou se precisar de botões e respostas a texto livre.
