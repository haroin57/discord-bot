"""Remote gateway imports must not load native DB/array libraries."""

import json
import os
import subprocess
import sys


def test_knowledge_search_import_does_not_load_native_libraries():
    source = """
import json, sys
from services.invest_kb import search_invest_kb, replace_document
from services.invest_kb.search import is_investment_query
print(json.dumps([name for name in ('pyarrow', 'lancedb', 'numpy', 'pandas', 'chromadb') if name in sys.modules]))
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=True, env=dict(os.environ), timeout=10)
    assert json.loads(result.stdout) == []
