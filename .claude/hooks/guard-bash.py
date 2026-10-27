#!/usr/bin/env python3
"""PreToolUse(Bash): barra o que o autor proibiu de forma permanente.

Duas familias, as duas por pedido explicito e repetido do autor:

1. `.env` — nunca ler, abrir, imprimir ou grepar. `.env.example` e liberado (e
   justamente a referencia publica). O `permissions.deny` ja cobre a ferramenta
   Read; isto fecha o caminho por shell, que e por onde escaparia.
2. git que ESCREVE — commit, push, branch, worktree. O autor commita e publica
   por conta propria; agente nenhum faz isso por ele. Leitura (status, log,
   diff, show) continua liberada, que e como o agente se orienta.

O `permissions.deny` casa por prefixo e nao enxerga `git -C <path> commit` nem
`x && git push`. Aqui a linha inteira e inspecionada, entao esses passam a ser
barrados tambem.

Sai com 2: a chamada e bloqueada e o stderr explica o motivo ao Claude.
"""

import json
import re
import sys

# `.env` como componente de caminho — pega `.env`, `./.env`, `app/.env.local`,
# `"$HOME/.env"`. O `(?!\.example)` preserva o unico arquivo liberado.
ENV = re.compile(r"(?<![\w.-])\.env(?!\.example\b)[\w.-]*")

GIT_WRITE = re.compile(
    r"""\bgit\b               # o comando
        (?:\s+-[^\s]+|\s+--[^\s]+(?:=[^\s]+)?|\s+-C\s+\S+)*  # flags globais (-C, -c, ...)
        \s+(commit|push|branch|worktree|switch|checkout)\b""",
    re.VERBOSE,
)
# `checkout`/`switch` so incomodam quando CRIAM branch; trocar de branch existente
# nao e o que o autor proibiu.
BRANCH_CREATING = re.compile(r"\b(?:checkout|switch)\b.*\s-(?:b|c|B|C)\b")

# `git branch` sozinho ou com flag de leitura so LISTA — e o comando que o agente
# usa para saber onde esta. So cria/apaga/renomeia quando recebe um nome ou uma
# destas flags.
BRANCH_WRITE_FLAG = re.compile(r"\s-(?:d|D|m|M|c|C)\b|--(?:delete|move|copy)\b")


def branch_only_lists(after: str) -> bool:
    # So o que vem DEPOIS de `branch`: o `-C` de `git -C <dir> branch` e a flag
    # global "rode como se estivesse em <dir>", nao o `-C` de copiar branch.
    if BRANCH_WRITE_FLAG.search(after):
        return False
    # Um argumento que nao comeca com `-` e o nome da branch a criar.
    return all(arg.startswith("-") for arg in after.split() if arg not in ("|", "&&"))


def verdict(command: str) -> str | None:
    if ENV.search(command):
        return (
            "BLOQUEADO: o comando toca um arquivo `.env`.\n"
            "O autor foi explicito e repetido: nunca ler, abrir nem analisar o "
            "`.env`. Use `.env.example` para saber quais variaveis existem, e "
            "pergunte ao autor o valor de que precisar."
        )

    match = GIT_WRITE.search(command)
    if not match:
        return None

    sub = match.group(1)
    if sub in ("checkout", "switch") and not BRANCH_CREATING.search(command):
        return None
    if sub == "branch" and branch_only_lists(command[match.end() :]):
        return None

    return (
        f"BLOQUEADO: `git {sub}` escreve no historico ou cria branch.\n"
        "O autor commita, publica e ramifica por conta propria — agente nenhum "
        "faz isso por ele. Deixe as mudancas na arvore de trabalho e diga o que "
        "mudou; leitura (`status`, `diff`, `log`, `show`) continua liberada."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command:
        return 0

    reason = verdict(command)
    if reason is None:
        return 0

    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
