"""Ported command modules, grouped by command ``type``.

* :mod:`.prompt_commands` — ``type: 'prompt'`` (template expanders)
* :mod:`.local_commands` — ``type: 'local'`` (headless computations)
* :mod:`.jsx_commands` — ``type: 'local-jsx'`` (interactive TUI, ported headlessly)

Each module exposes ``COMMANDS`` as a tuple of frozen, metadata-only
:class:`~free_code_commands.types.Command` instances. Private immutable definitions
are assembled into the registry without exposing callbacks or gate predicates.
"""
