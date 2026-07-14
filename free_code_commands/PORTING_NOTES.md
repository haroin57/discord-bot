# free_code_commands — porting contract

Python port of free-code's slash-command system (`/home/ubuntu/free-code/src/commands`).
Every ported command is constructed against the framework in `types.py` + `runtime.py`
and registered in `_all.py`. This file is the contract each command module follows.

## Framework API (import from `..types` / `..runtime`)

```python
from ..types import (
    Command, CommandType, ContentBlock, text_block,
    LocalCommand, PromptCommand, LocalJsxCommand,
    LocalCommandResult, JsxOutcome,
)
from ..runtime import CommandRuntime, ShellResult, Attribution
from ..shell import execute_shell_commands_in_prompt, substitute_arguments
```

### Command variants

* **local** → `LocalCommand(name, description, supports_non_interactive=<bool>, call_fn=<fn>)`
  where `call_fn(args: str, rt: CommandRuntime) -> LocalCommandResult` (may be `async`).
  Return `LocalCommandResult.text(value)` / `.skip()` / `.compact(result, display_text)`.

* **prompt** → `PromptCommand(name, description, progress_message=..., content_length=...,`
  `allowed_tools=[...], source='builtin', get_prompt_fn=<fn>)`
  where `get_prompt_fn(args: str, rt: CommandRuntime) -> list[ContentBlock]` (may be `async`).
  Use `text_block(text)` for `{type:'text', text}`. Expand embedded `` !`cmd` `` via
  `await execute_shell_commands_in_prompt(text, rt, '/name')`.

* **local-jsx** → `LocalJsxCommand(name, description, call_fn=<fn>)` where
  `call_fn(args, rt) -> JsxOutcome`. Interactive TUI is headless here: perform any non-UI
  effect through `rt` (e.g. `rt.open_dialog('theme')`) and return
  `JsxOutcome(dialog='theme', message=..., data={...})`. A pure dialog with no headless
  action can omit `call_fn` (defaults to `JsxOutcome(dialog=name)`).

### Shared metadata (all variants, keyword args)

`aliases=[...]`, `availability=['claude-ai'|'console']`, `argument_hint='<...>'`,
`immediate=True`, `is_sensitive=True`, `when_to_use='...'`, `_is_hidden=True` (static hidden),
`user_invocable=False`, `disable_model_invocation=True`, `loaded_from='...'`.

### Conditional enablement / hiding / dynamic description

TS `isEnabled` / `isHidden` / `get description()` read env, auth, feature flags. Port them as
callables taking the runtime:

```python
is_enabled_fn=lambda rt: not rt.env_truthy('DISABLE_X'),
is_hidden_fn=lambda rt: not rt.is_non_interactive_session(),   # rt has no such helper? add one
description_fn=lambda rt: f'... {rt.subscription_type()}',
```

Map TS globals to `CommandRuntime` methods (all overridable, safe defaults):
`rt.env(k)`, `rt.env_truthy(k)`, `rt.user_type()`, `rt.is_ant()`, `rt.platform()`, `rt.arch()`,
`rt.terminal()`, `rt.is_claude_ai_subscriber()`, `rt.is_consumer_subscriber()`,
`rt.has_anthropic_api_key_auth()`, `rt.subscription_type()`, `rt.is_overage_provisioning_allowed()`,
`rt.is_using_overage()`, `rt.feature('FLAG')`, `rt.policy_allowed('p')`, `rt.format_total_cost()`,
`rt.version()`, `rt.build_time()`, `await rt.get_default_branch()`, `rt.attribution_texts()`,
`await rt.get_enhanced_pr_attribution()`, `await rt.run_shell(cmd)`, `rt.messages()`,
`rt.open_dialog(name, **data)`, `rt.clear_conversation()`, `rt.set_vim_mode(b)`,
`rt.get_vim_mode()`, `rt.has_command(bin)`.

Feature-flag gated commands (bun `feature('X')`): gate with `is_enabled_fn=lambda rt: rt.feature('X')`.
USER_TYPE==='ant' gated: `is_enabled_fn=lambda rt: rt.is_ant()`.

## Fidelity rules

1. **Metadata verbatim**: copy `name`, `aliases`, `description`, `argumentHint` exactly from TS.
2. **Prompt templates verbatim**: copy the big prompt strings character-for-character. Preserve
   `` !`cmd` `` markers so `execute_shell_commands_in_prompt` expands them at runtime.
3. **Logic faithful**: port `call` bodies' branching (e.g. subscriber vs not, ant vs not).
4. **Runtime-bound / TUI**: if the real behaviour needs CLI internals that don't exist here,
   port metadata + a headless `call` that routes the intent through `rt` and returns a
   descriptive `JsxOutcome`/`LocalCommandResult`. Note the upstream file in a comment.
5. Every command module: add a top comment `# ported from src/commands/<path>`.

## Example (local, with subscriber branch — cost)

```python
# ported from src/commands/cost/cost.ts + cost/index.ts
def _cost_call(args, rt):
    if rt.is_claude_ai_subscriber():
        if rt.is_using_overage():
            value = ('You are currently using your overages to power your Claude Code '
                     'usage. We will automatically switch you back to your subscription '
                     'rate limits when they reset')
        else:
            value = 'You are currently using your subscription to power your Claude Code usage'
        if rt.is_ant():
            value += f'\n\n[ANT-ONLY] Showing cost anyway:\n {rt.format_total_cost()}'
        return LocalCommandResult.text(value)
    return LocalCommandResult.text(rt.format_total_cost())

cost = LocalCommand(
    name='cost',
    description='Show the total cost and duration of the current session',
    supports_non_interactive=True,
    is_hidden_fn=lambda rt: (False if rt.is_ant() else rt.is_claude_ai_subscriber()),
    call_fn=_cost_call,
)
```

## Example (prompt, with shell expansion — commit skeleton)

```python
# ported from src/commands/commit.ts
_ALLOWED = ['Bash(git add:*)', 'Bash(git status:*)', 'Bash(git commit:*)']

async def _commit_prompt(args, rt):
    attr = rt.attribution_texts().commit
    prefix = (rt.undercover_instructions() + '\n') if (rt.is_ant() and rt.is_undercover()) else ''
    text = f"""{prefix}## Context
- Current git status: !`git status`
...(verbatim)..."""
    final = await execute_shell_commands_in_prompt(text, rt, '/commit')
    return [text_block(final)]

commit = PromptCommand(name='commit', description='Create a git commit',
    allowed_tools=_ALLOWED, content_length=0, progress_message='creating commit',
    source='builtin', get_prompt_fn=_commit_prompt)
```

Note: `CommandRuntime` may be missing a helper a command needs (e.g.
`is_non_interactive_session`, `is_remote_mode`, `is_undercover`, `undercover_instructions`).
If so, add the method to `runtime.py` (both base default + `RecordingRuntime` override) rather
than working around it — keep the seam honest.
