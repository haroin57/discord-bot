"""Ported command modules, grouped by command ``type``.

* :mod:`.prompt_commands` — ``type: 'prompt'`` (template expanders)
* :mod:`.local_commands` — ``type: 'local'`` (headless computations)
* :mod:`.jsx_commands` — ``type: 'local-jsx'`` (interactive TUI, ported headlessly)

Each module exposes ``COMMANDS`` — a list of :class:`~free_code_commands.types.Command`
instances — which :mod:`free_code_commands._all` assembles into the registry.
"""
