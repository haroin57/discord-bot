"""SQLite persistence layer for DGM evolution data."""

from __future__ import annotations
import asyncio
import json
import sqlite3
import threading

from config import DGM_DB_PATH
from dgm.models import Agent, EvolutionRun

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DGM_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    parent_ids TEXT NOT NULL DEFAULT '[]',
    code TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    fitness REAL NOT NULL DEFAULT 0.0,
    scores TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    niche_x INTEGER NOT NULL DEFAULT 0,
    niche_y INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    population_size INTEGER NOT NULL,
    max_generations INTEGER NOT NULL,
    current_generation INTEGER NOT NULL DEFAULT 0,
    best_fitness REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'running'
);
"""

_MIGRATE_NICHE = """
ALTER TABLE agents ADD COLUMN niche_x INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN niche_y INTEGER NOT NULL DEFAULT 0;
"""


def _init_db_sync() -> None:
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    # Migrate: add niche columns if missing
    try:
        conn.execute("SELECT niche_x FROM agents LIMIT 1")
    except sqlite3.OperationalError:
        for stmt in _MIGRATE_NICHE.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
    conn.commit()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------

def _save_agent_sync(agent: Agent) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO agents (id, generation, parent_ids, code, strategy, fitness, scores, created_at, run_id, niche_x, niche_y)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               generation=excluded.generation,
               parent_ids=excluded.parent_ids,
               code=excluded.code,
               strategy=excluded.strategy,
               fitness=excluded.fitness,
               scores=excluded.scores,
               created_at=excluded.created_at,
               run_id=excluded.run_id,
               niche_x=excluded.niche_x,
               niche_y=excluded.niche_y""",
        (
            agent.id,
            agent.generation,
            json.dumps(agent.parent_ids),
            agent.code,
            agent.strategy,
            agent.fitness,
            json.dumps(agent.scores),
            agent.created_at,
            agent.run_id,
            agent.niche[0],
            agent.niche[1],
        ),
    )
    conn.commit()


async def save_agent(agent: Agent) -> None:
    await asyncio.to_thread(_save_agent_sync, agent)


def _row_to_agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        generation=row["generation"],
        parent_ids=json.loads(row["parent_ids"]),
        code=row["code"],
        strategy=row["strategy"],
        fitness=row["fitness"],
        scores=json.loads(row["scores"]),
        created_at=row["created_at"],
        run_id=row["run_id"],
        niche=(row["niche_x"], row["niche_y"]),
    )


def _load_agents_by_run_sync(run_id: str) -> list[Agent]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM agents WHERE run_id = ? ORDER BY generation, fitness DESC",
        (run_id,),
    ).fetchall()
    return [_row_to_agent(r) for r in rows]


async def load_agents_by_run(run_id: str) -> list[Agent]:
    return await asyncio.to_thread(_load_agents_by_run_sync, run_id)


def _load_best_agent_sync(run_id: str) -> Agent | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM agents WHERE run_id = ? ORDER BY fitness DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return _row_to_agent(row) if row else None


async def load_best_agent(run_id: str) -> Agent | None:
    return await asyncio.to_thread(_load_best_agent_sync, run_id)


def _load_agent_lineage_sync(agent_id: str) -> list[Agent]:
    conn = _get_conn()
    lineage: list[Agent] = []
    visited: set[str] = set()
    queue = [agent_id]
    while queue:
        aid = queue.pop(0)
        if aid in visited:
            continue
        visited.add(aid)
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
        if row is None:
            continue
        agent = _row_to_agent(row)
        lineage.append(agent)
        queue.extend(agent.parent_ids)
    return lineage


async def load_agent_lineage(agent_id: str) -> list[Agent]:
    return await asyncio.to_thread(_load_agent_lineage_sync, agent_id)


# ---------------------------------------------------------------------------
# Run CRUD
# ---------------------------------------------------------------------------

def _save_run_sync(run: EvolutionRun) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO runs (id, started_at, finished_at, population_size, max_generations, current_generation, best_fitness, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               started_at=excluded.started_at,
               finished_at=excluded.finished_at,
               population_size=excluded.population_size,
               max_generations=excluded.max_generations,
               current_generation=excluded.current_generation,
               best_fitness=excluded.best_fitness,
               status=excluded.status""",
        (
            run.id,
            run.started_at,
            run.finished_at,
            run.population_size,
            run.max_generations,
            run.current_generation,
            run.best_fitness,
            run.status,
        ),
    )
    conn.commit()


async def save_run(run: EvolutionRun) -> None:
    await asyncio.to_thread(_save_run_sync, run)


def _row_to_run(row: sqlite3.Row) -> EvolutionRun:
    return EvolutionRun(
        id=row["id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        population_size=row["population_size"],
        max_generations=row["max_generations"],
        current_generation=row["current_generation"],
        best_fitness=row["best_fitness"],
        status=row["status"],
    )


def _load_run_sync(run_id: str) -> EvolutionRun | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row else None


async def load_run(run_id: str) -> EvolutionRun | None:
    return await asyncio.to_thread(_load_run_sync, run_id)


def _load_latest_run_sync() -> EvolutionRun | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return _row_to_run(row) if row else None


async def load_latest_run() -> EvolutionRun | None:
    return await asyncio.to_thread(_load_latest_run_sync)
