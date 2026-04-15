#!/usr/bin/env python3
"""Standalone DGM runner — executes evolution and prints progress."""

import asyncio
import logging
import sys
import os

# Setup path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from dgm import engine
from dgm.persistence import init_db


async def main():
    init_db()

    async def progress(msg: str):
        print(msg, flush=True)

    print("🧬 Darwin Gödel Machine 起動", flush=True)
    print("個体群: 4 | 世代数: 3", flush=True)
    print("進化を開始します...\n", flush=True)

    try:
        run = await engine.run_evolution(
            population_size=4,
            max_generations=3,
            progress_callback=progress,
        )
        print(f"\n完了 — Status: {run.status}, Best fitness: {run.best_fitness:.1%}")
    except Exception as e:
        print(f"\n❌ 失敗: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
