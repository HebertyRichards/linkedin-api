@AGENTS.md

# Claude Code

Everything above applies. This part covers only what is specific to Claude Code in this repo.

## Guardrails in `.claude/`

- `settings.json` denies reading or editing `.env` files and running `git commit`, `git push` and `git worktree`.
- `hooks/guard-bash.py` checks the whole Bash line, so it also blocks `.env` through the shell, `git -C <path> commit`,
  `x && git push`, and creating a branch (`checkout -b`, `switch -c`, `git branch <name>`).
- A blocked call is a project rule, not an obstacle: do not look for another way around it. Leave the changes in the
  working tree and say what changed. Use `.env.example` for variable names.
- The hook reads the full command text: a test or heredoc that contains one of those words is blocked too. Put such
  test input in a file outside the repo and pass its path.

## Skills for this repo

- **Bug fix:** `red-green-proof` - the new test must fail without the fix and pass with it.
- **`SCOPE.md`, `CONTEXT.md` or `docs/adr/` changed:** run the `project-init` check (`project_docs.py check .`); new
  ADRs come from its `adr` command.
- **Before a release:** `secrets-guard`, then `make check`.

## Probing a live source

When a source's page or JSON must be checked against reality, use a generic search term (never personal data), save the
answer to a new folder under the OS temp dir and parse it with `python -I`. Write down what was seen in `CONTEXT.md` or
an ADR, never the raw page.
