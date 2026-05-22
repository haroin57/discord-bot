"""Validate and apply an LLM emotion-engine output to an EmotionState.

v3 stance: trust the LLM. The validator is a thin shim that only does
what code is uniquely good at:

  - Coerce numerics + BOUNDS clamp (cheap physical-domain guard)
  - Stamp metadata the LLM shouldn't compute (timestamps, topics)
  - Capture the LLM-supplied labels + emotion-journal entry

Earlier drafts also rolled back "large unjustified jumps" and pinned a
floor on loneliness during silence. Both were dropped 2026-06-14: they
suppressed the very unpredictability the LLM transition was designed
for, and the dropped floor explicitly accepts the risk that kanae may
fall silent for arbitrarily long if the model keeps reporting low
loneliness.

v4 stance (2026-05-16, EMOTION_DYNAMICS_V2): an optional post-clamp
*dynamics layer* may perturb numerics for anti-flatness, circadian
pressure, and tick jitter (see services/emotion/dynamics.py). To
preserve the v3 invariant "displayed label never disagrees with numeric
value" (format.py:1-12), any axis perturbed beyond LABEL_RESET_THRESHOLD
has its corresponding ``state.labels[axis]`` cleared — the renderer's
"普通" fallback shows for one tick until the next LLM call re-labels
coherently. Numeric drift without re-labeling would re-introduce
exactly the disagreement v3 deleted.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone

from services.emotion.params import BOUNDS
from services.emotion.state import EmotionState, LastInteraction

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_NUMERIC_FIELDS = (
    "valence", "arousal", "loneliness", "affection", "curiosity", "anxiety",
)


def validate_and_apply(
    state: EmotionState,
    llm_output: dict,
    *,
    mode: str,
    conversation_meta: dict | None = None,
    # `silence_hours` kept as a keyword-only no-op so callers built
    # against the older signature don't blow up. Unused — see module docstring.
    silence_hours: float = 0.0,
) -> EmotionState | None:
    """Fold `llm_output` into `state`, returning the new state (or None).

    Returns None when the output is structurally invalid (missing
    numerics, non-numeric values) so callers can keep the previous state
    rather than save garbage.

    Args:
        state: Current state (becomes the previous frame).
        llm_output: Parsed JSON from the LLM. Required keys: valence,
                    arousal, loneliness, affection, curiosity, anxiety.
                    Optional: trigger, reasoning, event_summary,
                    derived_labels (list[str]), labels (dict[str,str]).
        mode: "conversation" or "silence".
        conversation_meta: For conversation mode, a dict carrying
                           ``topics``, ``quality``, ``senpai_engagement``
                           to stamp onto ``last_interaction``. May be
                           omitted; sane defaults are used.
    """
    del silence_hours  # intentionally unused, see module docstring

    # 1. Coerce numerics. Missing or non-float → reject the whole turn.
    numerics: dict[str, float] = {}
    for key in _NUMERIC_FIELDS:
        if key not in llm_output:
            log.warning("validator: missing field %s", key)
            return None
        try:
            numerics[key] = float(llm_output[key])
        except (TypeError, ValueError):
            log.warning("validator: non-numeric %s=%r", key, llm_output[key])
            return None

    # 2. Clamp to physical bounds (the only numeric guardrail we keep).
    for key, value in numerics.items():
        lo, hi = BOUNDS[key]
        numerics[key] = max(lo, min(hi, value))

    # 2.5 v4 dynamics: optional post-LLM perturbation. Skipped when the LLM
    # already produced a large jump (don't double-correct semantic moves).
    # Returns the set of axes moved beyond LABEL_RESET_THRESHOLD so we can
    # invalidate their LLM-supplied labels later (step 3.5).
    perturbed_axes: set[str] = set()
    from services.emotion.params import EMOTION_DYNAMICS_V2
    if EMOTION_DYNAMICS_V2:
        from services.emotion.dynamics import max_axis_jump, perturb
        from services.emotion.params import LARGE_LLM_JUMP_THRESHOLD
        now_jst = datetime.now(JST)
        if max_axis_jump(state, numerics) <= LARGE_LLM_JUMP_THRESHOLD:
            numerics, perturbed_axes = perturb(
                numerics,
                prev_state=state,
                journal=state.emotion_journal,
                now=now_jst,
                mode=mode,
            )

    # 3. Build the new state.
    now = datetime.now(JST)
    new_state = dataclasses.replace(
        state,
        valence=numerics["valence"],
        arousal=numerics["arousal"],
        loneliness=numerics["loneliness"],
        affection=numerics["affection"],
        curiosity=numerics["curiosity"],
        anxiety=numerics["anxiety"],
        updated_at=now.isoformat(),
    )

    # Labels (renderer reads these directly — no algebraic bucketing).
    raw_derived = llm_output.get("derived_labels") or []
    if isinstance(raw_derived, list):
        new_state.derived_labels = [str(s) for s in raw_derived if s][:3]
    raw_labels = llm_output.get("labels") or {}
    if isinstance(raw_labels, dict):
        new_state.labels = {str(k): str(v) for k, v in raw_labels.items() if v}

    # 3.5 v4 dynamics: clear labels for axes that v4 perturb moved
    # significantly. The "普通" fallback in format.py shows for one tick;
    # the next LLM call re-labels coherently with the new numeric value.
    if perturbed_axes:
        new_state.labels = {
            k: v for k, v in new_state.labels.items() if k not in perturbed_axes
        }

    # Emotion journal ring buffer (25 entries per v3 §4.3).
    trigger = (llm_output.get("trigger") or "").strip()
    journal = list(state.emotion_journal)
    journal.append({
        "ts": now.isoformat(),
        "v": numerics["valence"],
        "ar": numerics["arousal"],
        "lo": numerics["loneliness"],
        "af": numerics["affection"],
        "cu": numerics["curiosity"],
        "ax": numerics["anxiety"],
        "trigger": (trigger or str(llm_output.get("event_summary", ""))[:20])[:40],
        "source": mode,
    })
    new_state.emotion_journal = journal[-25:]

    # 4. Conversation-mode metadata: timestamps & topic capture for telos.
    if mode == "conversation":
        meta = conversation_meta or {}
        topics_raw = meta.get("topics") or []
        if not isinstance(topics_raw, list):
            topics_raw = []
        new_state = dataclasses.replace(
            new_state,
            last_interaction=LastInteraction(
                timestamp=now.isoformat(),
                quality=_clamp01(meta.get("quality", 0.7)),
                initiated_by="senpai",
                topics=[str(t) for t in topics_raw][:5],
                duration_minutes=0.0,
                senpai_responsiveness=_clamp01(meta.get("senpai_engagement", 0.7)),
            ),
        )

    return new_state


def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5
