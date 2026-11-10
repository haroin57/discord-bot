"""Load pure MCP contracts even in legacy hosts with an older alpha installed."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys

try:
    contracts = importlib.import_module("llm_mem0.api_contracts")
except ModuleNotFoundError as exc:
    if exc.name not in {"llm_mem0", "llm_mem0.api_contracts"}:
        raise
    # A bot source generation owns this fallback; no auth/client module is
    # executed and no mutable external checkout is imported.
    name = "_bot_mem0_api_contracts"
    contracts = sys.modules.get(name)
    if contracts is None:
        path = Path(__file__).resolve().parents[2] / "vendor/mem0-alpha/llm_mem0/api_contracts.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("bundled Mem0 API contracts unavailable")
        contracts = importlib.util.module_from_spec(spec)
        sys.modules[name] = contracts
        spec.loader.exec_module(contracts)
