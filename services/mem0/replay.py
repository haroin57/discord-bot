"""Sleep-like systems consolidation — Phase ③.

Two systems already exist in this bot: ``history_index`` (the raw,
verbatim transcript — a hippocampus-like fast store of everything that was
said) and the curated Mem0/Chroma fact store (a slower, distilled
neocortex-like store). Before this module, the only transfer between them
happened once, at ingest time, on the turn as it was written. Human systems
consolidation instead replays hippocampal traces repeatedly (classically
during sleep) to both re-encode what the fast store already held and to
extract higher-level regularities the fast store never explicitly
represented. This module gives two batch operations that mirror that:

  1. **Replay** (``replay_channel_history``): re-run extraction over OLD raw
     history using the CURRENT entity dictionary. A proper noun mentioned
     once, early on — before it had enough later mentions to seed
     ``graph.get_top_entities`` — was invisible to the extractor at the time
     it was said. Replaying it later, once the name is "known", can recover
     the fact retroactively. Facts already known are absorbed as
     reinforcement by the existing dedup pipeline, not duplicated.

  2. **Schema abstraction** (``abstract_schemas_for_user``): human semantic
     memory holds general regularities ("likes science fiction") separately
     from the individual episodes that support them ("likes Blade Runner",
     "likes Harmony"). This clusters facts that each reference a DIFFERENT
     entity sharing a (category, subcategory, entity type), and — only when
     enough independent instances clearly support it — synthesizes a
     general-trend fact via the small model. Instances are never archived;
     the schema fact coexists as a higher-level summary.

Not the same system as ``scripts/mem0_consolidate.py`` (ADR-0016): that
script clusters facts by EMBEDDING similarity to merge near-duplicate
wording regardless of age. This module recovers facts the extractor missed
the first time and generalizes across distinct instances — a different
axis, not a competing implementation.

Both operations are dry-run by default (config.py) and only ever ADD facts
(replay's absorbed-as-reinforcement case aside, which only touches
counters) — neither can destroy information, unlike decay.py's gist
compression.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

REPLAY_MAX_TURNS_PER_RUN = int(os.environ.get("MEM0_REPLAY_MAX_TURNS_PER_RUN", "50"))
REPLAY_WORK_LEASE_SECONDS = max(
    0.001,
    float(os.environ.get("MEM0_REPLAY_WORK_LEASE_SECONDS", "30")),
)
REPLAY_WORK_HEARTBEAT_INTERVAL = max(
    0.001,
    float(
        os.environ.get(
            "MEM0_REPLAY_WORK_HEARTBEAT_INTERVAL",
            str(min(1.0, REPLAY_WORK_LEASE_SECONDS / 3)),
        ),
    ),
)
SCHEMA_MIN_DISTINCT_ENTITIES = int(
    os.environ.get("MEM0_SCHEMA_MIN_DISTINCT_ENTITIES", "3")
)
SCHEMA_MAX_GROUPS_PER_RUN = int(os.environ.get("MEM0_SCHEMA_MAX_GROUPS_PER_RUN", "10"))


# ---------------------------------------------------------------------------
# (a) Replay
# ---------------------------------------------------------------------------
def _cursor_path() -> Path:
    from config import STATE_DIR

    return Path(STATE_DIR) / "replay_cursor.json"


def _load_cursor() -> dict:
    p = _cursor_path()
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("replay cursor load failed: %s", exc)
        return {}


def _save_cursor(data: dict) -> None:
    from utils.atomic_write import atomic_write_json

    atomic_write_json(_cursor_path(), data)


def _pair_turns(lines: list[dict]) -> tuple[list[tuple[str, str]], str]:
    """Pair adjacent (user, assistant) lines into turns for re-extraction.

    ``channel``-role lines (ambient messages not addressed to/from the
    primary speaker) are skipped entirely — replay must not introduce
    speaker-bleed that the live ingest path already guards against. Returns
    ``(turns, last_ts_seen)`` so the caller can advance the cursor even past
    trailing lines that didn't form a complete turn.
    """
    turns: list[tuple[str, str]] = []
    last_ts = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        last_ts = line["ts"] or last_ts
        if line["role"] != "user":
            i += 1
            continue
        user_text = line["text"]
        assistant_text = ""
        if i + 1 < n and lines[i + 1]["role"] == "assistant":
            assistant_text = lines[i + 1]["text"]
            last_ts = lines[i + 1]["ts"] or last_ts
            i += 2
        else:
            i += 1
        turns.append((user_text, assistant_text))
    return turns, last_ts


def _pair_turn_work(lines: list[dict]) -> list[tuple[str, str, str]]:
    """Pair turns with a cursor covering skipped rows before the next turn."""
    turns: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line["role"] != "user":
            i += 1
            continue
        user_text = line["text"]
        assistant_text = ""
        cursor = line.get("ts") or ""
        if i + 1 < len(lines) and lines[i + 1]["role"] == "assistant":
            assistant_text = lines[i + 1]["text"]
            cursor = lines[i + 1].get("ts") or cursor
            i += 2
        else:
            i += 1
        while i < len(lines) and lines[i]["role"] != "user":
            cursor = lines[i].get("ts") or cursor
            i += 1
        turns.append((user_text, assistant_text, cursor))
    return turns


class _WorkLeaseHeartbeat:
    """Keep a replay work claim live and expose a provider-side-effect fence."""

    def __init__(
        self,
        repository,
        lease,
        *,
        lease_seconds: float,
        interval_seconds: float,
    ) -> None:
        self.repository = repository
        self.lease = lease
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.lost = False
        self._task: asyncio.Task | None = None

    async def _renew(self) -> bool:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self.repository.renew_work,
                self.lease,
                lease_seconds=self.lease_seconds,
            ),
            name=f"mem0-replay-renew-{self.lease.id}",
        )
        try:
            return bool(await asyncio.shield(operation))
        except asyncio.CancelledError:
            # A worker thread cannot be cancelled. Drain it before cleanup so
            # it cannot renew the claim after the owner has returned it.
            await _complete_cleanup(operation)
            raise

    async def start(self) -> bool:
        if not await self._renew():
            self.lost = True
            return False
        self._task = asyncio.create_task(
            self._run(),
            name=f"mem0-replay-heartbeat-{self.lease.id}",
        )
        return True

    async def fence(self) -> bool:
        """Verify token and expiry immediately before the provider call."""
        if self.lost or not await self._renew():
            self.lost = True
            return False
        return True

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_seconds)
                try:
                    renewed = await self._renew()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception(
                        "replay work lease renewal failed for %s: %s",
                        self.lease.id,
                        exc,
                    )
                    renewed = False
                if not renewed:
                    self.lost = True
                    return
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _complete_cleanup(awaitable):
    """Finish cancellation cleanup even if the owner receives another cancel."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _run_sync_complete(func, /, *args, **kwargs):
    """Drain a synchronous DB operation before propagating cancellation."""
    operation = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await _complete_cleanup(operation)
        raise


async def replay_channel_history(
    channel_id: int,
    user_id: str,
    *,
    max_turns: int | None = None,
    dry_run: bool = True,
) -> dict:
    """Re-run fact extraction over old history for one channel, walking
    forward from a per-channel cursor (persisted in
    ``STATE_DIR/replay_cursor.json``) so repeated nightly runs make forward
    progress through a backlog instead of reprocessing the same window.

    Returns ``{"turns_processed": int, "facts_kept": int, "cursor_advanced_to": str}``.
    In dry-run the cursor is never advanced (a failed/aborted dry-run run
    must not silently skip a window on the next real run).
    """
    from services.mem0 import history_index
    from services.mem0.ingest import add_memories
    from services.mem0.integrity import get_application_service, get_repository

    if max_turns is None:
        max_turns = REPLAY_MAX_TURNS_PER_RUN

    repository = get_repository()
    partition = str(channel_id)
    cursor = _load_cursor()
    since_ts = repository.work_cursor("replay", partition) or cursor.get(partition, "")
    lines = await asyncio.to_thread(
        history_index.get_lines_since,
        channel_id,
        since_ts,
        max(max_turns * 4, 1),
    )
    work = _pair_turn_work(lines)[:max_turns]

    if dry_run:
        return {
            "turns_processed": len(work),
            "facts_kept": 0,
            "cursor_advanced_to": since_ts,
        }

    for user_text, assistant_text, turn_cursor in work:
        repository.enqueue_work(
            "replay",
            partition,
            turn_cursor,
            {
                "channel_id": channel_id,
                "user_id": user_id,
                "user_text": user_text,
                "assistant_text": assistant_text,
            },
        )
    if not work and lines:
        last_seen = next(
            (line.get("ts") for line in reversed(lines) if line.get("ts")),
            since_ts,
        )
        repository.enqueue_work(
            "replay",
            partition,
            last_seen,
            {"channel_id": channel_id, "user_id": user_id, "skip": True},
        )

    processed = 0
    facts_kept = 0
    worker_id = f"replay-{os.getpid()}-{id(asyncio.current_task())}"
    service = get_application_service()
    while processed < max_turns:
        lease_operation = asyncio.create_task(
            asyncio.to_thread(
                repository.lease_work,
                "replay",
                worker_id,
                lease_seconds=REPLAY_WORK_LEASE_SECONDS,
                partition_key=partition,
            ),
            name="mem0-replay-lease",
        )
        try:
            leased = await asyncio.shield(lease_operation)
        except asyncio.CancelledError:
            claimed = await _complete_cleanup(lease_operation)
            for abandoned in claimed:
                await _complete_cleanup(
                    asyncio.to_thread(
                        repository.retry_work,
                        abandoned,
                        "replay worker cancelled during lease",
                    ),
                )
            raise
        if not leased:
            break
        lease = leased[0]
        heartbeat = _WorkLeaseHeartbeat(
            repository,
            lease,
            lease_seconds=REPLAY_WORK_LEASE_SECONDS,
            interval_seconds=REPLAY_WORK_HEARTBEAT_INTERVAL,
        )
        cancellation_disposed = False
        try:
            if not await heartbeat.start():
                log.warning("replay: lease was lost before processing: %s", lease.key)
                break
            if not await heartbeat.fence():
                log.warning(
                    "replay: lease was lost before provider call: %s", lease.key
                )
                break
            provider_operation = asyncio.create_task(
                service.replay(
                    {**lease.payload, "idempotency_key": lease.key},
                    add_memories,
                ),
                name=f"mem0-replay-provider-{lease.id}",
            )
            try:
                result = await asyncio.shield(provider_operation)
            except asyncio.CancelledError as cancelled:
                # ``add_memories`` ultimately runs provider calls in
                # ``to_thread``. Cancelling this coroutine cannot stop those
                # side effects. Retain and drain the task while the heartbeat
                # keeps the work lease live, then commit the completed result
                # exactly as the non-cancelled path would.
                try:
                    result = await _complete_cleanup(provider_operation)
                    if isinstance(result, dict) and result.get("error"):
                        raise RuntimeError(result["error"])
                except BaseException as exc:
                    await _complete_cleanup(
                        asyncio.to_thread(repository.retry_work, lease, exc),
                    )
                else:
                    if heartbeat.lost:
                        log.warning(
                            "replay: lease was lost after provider completion: %s",
                            lease.key,
                        )
                    else:
                        acked = await _complete_cleanup(
                            asyncio.to_thread(repository.ack_work, lease),
                        )
                        if acked:
                            cursor[partition] = lease.cursor_value
                            await _complete_cleanup(
                                asyncio.to_thread(_save_cursor, cursor),
                            )
                        else:
                            log.warning(
                                "replay: lease was lost before cancelled ack: %s",
                                lease.key,
                            )
                await _complete_cleanup(heartbeat.close())
                cancellation_disposed = True
                raise cancelled
            if heartbeat.lost:
                raise RuntimeError(f"lost replay work lease {lease.id}")
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(result["error"])
            acked = await _run_sync_complete(repository.ack_work, lease)
            if not acked:
                log.warning("replay: lease was lost before ack: %s", lease.key)
                break
            if not lease.payload.get("skip"):
                processed += 1
            if isinstance(result, dict):
                facts_kept += result.get("kept", 0) or 0
            cursor[partition] = lease.cursor_value
            _save_cursor(cursor)
        except asyncio.CancelledError:
            await _complete_cleanup(heartbeat.close())
            if not cancellation_disposed:
                await _complete_cleanup(
                    asyncio.to_thread(
                        repository.retry_work,
                        lease,
                        "replay worker cancelled",
                    ),
                )
            raise
        except Exception as exc:
            await heartbeat.close()
            await asyncio.to_thread(repository.retry_work, lease, exc)
            log.warning("replay: leased item failed and was retained: %s", exc)
            break
        finally:
            await heartbeat.close()

    log.info(
        "[REPLAY channel=%s user=...%s turns=%d kept=%d dry_run=%s]",
        channel_id,
        str(user_id)[-4:],
        processed,
        facts_kept,
        dry_run,
    )

    return {
        "turns_processed": processed,
        "facts_kept": facts_kept,
        "cursor_advanced_to": repository.work_cursor("replay", partition) or since_ts,
    }


# ---------------------------------------------------------------------------
# (b) Schema abstraction
# ---------------------------------------------------------------------------
_SCHEMA_ABSTRACT_PROMPT = """以下は同じ話題領域に属する、ユーザー本人についての独立した具体的事実です。
これらに共通する一般的な傾向として明確に支持できる要約(スキーマ)があれば生成してください。

事実一覧:
{facts}

== ルール ==
- 個々の事実の多くが同じ一般化を明確に支持する場合のみ生成する
- 過度に一般化しない (例:「SF作品が好き」は妥当、「映画全般が好き」は過度な一般化)
- 個々の具体的事実は消えず並存するので、要約は「傾向の補足」であって「置き換え」ではない
- 支持が弱い/迷う場合は生成しない(空配列で構わない)
- 日本語1文、120字以内

以下のJSONのみ返してください(説明不要):
{{"schemas": [{{"text": "<一般化した1文>", "confidence": "<high|medium>"}}]}}"""


async def _generate_schema_facts(instance_texts: list[str]) -> list[str]:
    """Ask the small model for 0..n supported generalizations. Fail-open to
    an empty list — an abstraction opportunity missed this run is picked up
    the next time the group is still eligible; a hallucinated overgeneralization
    inserted into long-term memory is much harder to undo."""
    try:
        from services.mem0.client import get_anthropic_client
        from services.mem0.cli_compat import cli_messages_create, extract_text
        from services.mem0.format import strip_json_fence

        client = await get_anthropic_client()
        if client is None:
            return []
        facts_block = "\n".join(
            f"- {t[:200].replace('{', '{{').replace('}', '}}')}" for t in instance_texts
        )
        resp = await cli_messages_create(
            client,
            model=os.environ.get("MEM0_SCHEMA_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=int(os.environ.get("MEM0_SCHEMA_MAX_TOKENS", "500")),
            user_message=_SCHEMA_ABSTRACT_PROMPT.format(facts=facts_block),
            task_prompt=(
                "You generalize a cluster of concrete personal facts into a "
                "clearly-supported trend statement, or emit none if "
                "unsupported. Return JSON only."
            ),
        )
        text = strip_json_fence(extract_text(resp))
        if not text:
            return []
        data = json.loads(text)
        out = []
        for item in data.get("schemas", []):
            if not isinstance(item, dict):
                continue
            t = (item.get("text") or "").strip()
            if t and item.get("confidence") in ("high", "medium"):
                out.append(t)
        return out
    except Exception as exc:
        log.warning("schema abstraction generation failed (non-fatal): %s", exc)
        return []


async def abstract_schemas_for_user(
    user_id: str,
    *,
    dry_run: bool = True,
    max_groups: int | None = None,
) -> dict:
    """Cluster facts by (category, subcategory, entity type) — keeping at
    most one fact per DISTINCT linked entity per group, so a group only
    qualifies when it spans several different things, not several mentions
    of the same thing (that's dedup's job, not this module's) — and, for
    groups with at least ``SCHEMA_MIN_DISTINCT_ENTITIES`` members, ask the
    model for a supported generalization.

    New schema facts are inserted through the normal ``add_memories``
    dedup/gate pipeline via its ``pre_extracted_facts`` escape hatch, so a
    schema that already exists (or nearly does) gets absorbed/reinforced
    rather than duplicated. Tagged ``source=consolidation_schema`` and
    ``derived_from=<comma-joined instance fact ids>`` for provenance.
    """
    import asyncio
    from services.mem0 import client as client_mod
    from services.mem0 import graph
    from services.mem0.ingest import add_memories

    if max_groups is None:
        max_groups = SCHEMA_MAX_GROUPS_PER_RUN

    rows = await client_mod.get_all_memories(user_id)
    groups: dict[tuple, dict[int, dict]] = {}
    for r in rows:
        md = r.get("metadata") or {}
        if not isinstance(md, dict):
            continue
        if (
            md.get("valid_to")
            or md.get("gist_of")
            or "schema" in (md.get("tags") or "")
        ):
            continue
        fid = r.get("id")
        if not fid:
            continue
        entities = await asyncio.to_thread(graph.get_fact_entities, fid)
        if not entities:
            continue
        key_entity = max(entities, key=lambda e: e.mention_count)
        key = (md.get("category"), md.get("subcategory"), key_entity.type)
        by_entity = groups.setdefault(key, {})
        by_entity.setdefault(key_entity.id, r)

    result = {"groups_checked": 0, "schemas_created": 0}
    eligible = {
        k: v for k, v in groups.items() if len(v) >= SCHEMA_MIN_DISTINCT_ENTITIES
    }
    for key, by_entity in list(eligible.items())[:max_groups]:
        result["groups_checked"] += 1
        members = list(by_entity.values())
        texts = [
            t for t in (m.get("memory") or m.get("text") or "" for m in members) if t
        ]
        if len(texts) < SCHEMA_MIN_DISTINCT_ENTITIES:
            continue
        schema_texts = await _generate_schema_facts(texts)
        if not schema_texts:
            continue
        member_ids = [m.get("id") for m in members if m.get("id")]
        log.info(
            "[SCHEMA_ABSTRACT user=...%s key=%s instances=%d schemas=%d dry_run=%s] %r",
            str(user_id)[-4:],
            key,
            len(members),
            len(schema_texts),
            dry_run,
            schema_texts,
        )
        if dry_run:
            result["schemas_created"] += len(schema_texts)
            continue

        category, subcategory, _etype = key
        for schema_text in schema_texts:
            fact = {
                "speaker": "self",
                "text": schema_text,
                "importance": 3,
                "category": category or "meta",
                "subcategory": subcategory or "",
                "tags": ["schema"],
                "attribute": "",
                "condition": "",
                "scope": "global",
                "entities": [],
            }
            try:
                res = await add_memories(
                    user_id=user_id,
                    pre_extracted_facts=[fact],
                    metadata={
                        "source": "consolidation_schema",
                        "derived_from": ",".join(member_ids),
                    },
                )
            except Exception as exc:
                log.warning("schema insert failed (non-fatal): %s", exc)
                continue
            if isinstance(res, dict):
                result["schemas_created"] += res.get("kept", 0) or 0
    return result
