# free_code_commands

A faithful **Python port of free-code's slash-command system** (the `src/commands`
tree of a Claude-Code-family TypeScript/Bun CLI). Standalone — it does not import
any Discord-bot internals — so it can be tested, embedded, or driven headlessly.

## What was ported

| Layer | File | Upstream |
|---|---|---|
| Command model | `types.py` | `src/types/command.ts` |
| DI runtime seam | `runtime.py` | `ToolUseContext` + module globals |
| Prompt expansion | `shell.py` | `utils/promptShellExecution.ts` |
| Frontmatter | `frontmatter.py` | slash-command `allowed-tools` parsing |
| Registry / dispatch | `registry.py` | `commands.ts` (findCommand/getCommand/getCommands) |
| Commands | `commands/*.py` | `src/commands/**` |
| Assembly | `_all.py` | the `COMMANDS` array in `commands.ts` |

**112 commands** total, grouped by upstream `type`:

- `commands/prompt_commands.py` — **9** `prompt` commands (commit, commit-push-pr,
  review, pr-comments, init, init-verifiers, security-review, statusline, insights).
  Templates are stored verbatim under `_prompts/`.
- `commands/local_commands.py` — **19** `local` commands (cost, version, clear,
  compact, context, vim, advisor, bridge-kick, …).
- `commands/jsx_commands.py` — **51** `local-jsx` (interactive) commands, ported
  headlessly: metadata + a `call` that records which dialog would open.
- `commands/feature_gated.py` — **12** flag-gated commands (`is_enabled_fn` gated on
  `rt.feature('FLAG')`), conditionally registered in `commands.ts`.
- `commands/internal_commands.py` — **21** ant-only `INTERNAL_ONLY_COMMANDS`.

## Fidelity notes (honest gaps)

This distribution ships some commands as **stubs / stripped source**, and the port
reflects that rather than inventing behaviour:

- Every `INTERNAL_ONLY` command's upstream `index.js` is the shim
  `{ isEnabled: () => false, isHidden: true, name: 'stub' }` — the real ant tooling
  is not in the public build. They are ported as disabled + hidden stubs under their
  canonical names (so the enumeration is complete).
- Several feature-gated commands (`fork`, `buddy`, `peers`, `workflows`, `assistant`,
  `proactive`, `torch`, `remote-control-server`) have their source dirs stripped; they
  are represented as flag-gated metadata shells.
- Runtime-bound behaviour (real compaction, OAuth, Ink rendering, git) is declared as
  `CommandRuntime` methods with inert defaults — the seam is honest about what is
  delegated vs. computed here.

## Usage

```python
from free_code_commands import build_registry
from free_code_commands.runtime import CommandRuntime  # subclass to wire real services

reg = build_registry()
cmd, args = reg.parse("/commit")          # or reg.find("commit") / reg.resolve("commit", rt)
# prompt commands:
blocks = await cmd.get_prompt_for_command(args, rt)   # list[ContentBlock]
# local commands:
result = await cmd.call(args, rt)                     # LocalCommandResult
```

See `PORTING_NOTES.md` for the per-command construction contract, and
`tests/test_free_code_commands_*.py` (92 PoC tests) for behavioural parity coverage.
