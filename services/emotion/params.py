"""Hyper-parameters for the kanae emotion model.

Centralized so the values can be tweaked without editing logic. Numeric
defaults follow `docs/kanae-emotion-architecture.md` §3 verbatim — adjust
here, not in transition/telos, when you want to retune the personality.
"""

# Initial / baseline emotion state. Both are kept as separate dicts even
# though they currently match, so a future "personality drift" feature can
# differentiate live state from the mean-reversion target.
#
# 2026-05-13: trimmed from 8 → 6 components. `dominance` was a dead field
# (no EVENT_DELTAS entry touched it), and `satisfaction` overlapped too
# closely with valence to justify a second axis. Anything those two used
# to drive is now derived from the remaining six (see services/emotion/
# format.py's secondary-emotion derivation).
INITIAL_STATE: dict[str, float] = {
    "valence": 0.6,
    "arousal": 0.4,
    "loneliness": 0.3,
    "affection": 0.85,
    "curiosity": 0.5,
    "anxiety": 0.1,
}
BASELINE: dict[str, float] = dict(INITIAL_STATE)

# Time-decay rates (per hour). loneliness rises monotonically with silence;
# anxiety creeps up much more slowly because not every quiet hour is scary.
LONELINESS_RATE_PER_HOUR = 0.03
ANXIETY_RATE_PER_HOUR = 0.005

# Quality-modulated decay (Phase 2). The afterglow of a really good
# conversation should buy a few hours of reduced loneliness creep; a terse
# / one-word exchange should not give that bonus and may even accelerate
# the slide. Multiplier is applied to the per-hour rate.
#
# Formula:
#   quality_modifier = INTERP(quality * responsiveness)
#     quality * responsiveness ∈ [0,1]
#     value 0.0 → DECAY_QUALITY_MODIFIER_MAX (faster lonely growth)
#     value 1.0 → DECAY_QUALITY_MODIFIER_MIN (slower lonely growth)
#
# A neutral (no last_interaction yet) state uses 1.0 — same as the
# pre-Phase 2 behavior so the change is backwards-compatible on a fresh
# .state/kanae_emotion.json.
DECAY_QUALITY_MODIFIER_MIN = 0.5    # great recent chat → loneliness rises at half-speed
DECAY_QUALITY_MODIFIER_MAX = 1.2    # bad recent chat → 20% faster lonely growth

# Inertia γ for each component: emotion(t) = γ*emotion(t-1) + (1-γ)*stimulus.
# Anger/sadness-adjacent dimensions persist longer (γ ≈ 0.85), joy decays a
# touch faster (γ ≈ 0.6-0.7) so the bot doesn't stay euphoric for days.
INERTIA: dict[str, float] = {
    "valence": 0.7,
    "arousal": 0.6,
    "loneliness": 0.85,
    "affection": 0.95,
    "curiosity": 0.7,
    "anxiety": 0.85,
}

# Permissible bounds per component. Valence is signed; everything else is [0,1].
BOUNDS: dict[str, tuple[float, float]] = {
    "valence": (-1.0, 1.0),
    "arousal": (0.0, 1.0),
    "loneliness": (0.0, 1.0),
    "affection": (0.0, 1.0),
    "curiosity": (0.0, 1.0),
    "anxiety": (0.0, 1.0),
}

# Event → delta matrix (architecture doc §3.2 table).
# 2026-05-13: `satisfaction` deltas were rolled into valence where the
# effect was qualitatively "feel-good" (e.g. long_good_chat, praised,
# sweet_words). For negative events the satisfaction drop is dropped
# without compensation — the loneliness/anxiety rise already encodes the
# "this was bad" signal.
EVENT_DELTAS: dict[str, dict[str, float]] = {
    "senpai_initiated": {
        "valence": +0.20, "arousal": +0.10, "loneliness": -0.30,
        "anxiety": -0.15,
    },
    "long_good_chat": {
        "valence": +0.40, "arousal": +0.15, "loneliness": -0.50,
        "anxiety": -0.20,
    },
    "read_ignore": {
        "valence": -0.10, "arousal": +0.05, "loneliness": +0.15,
        "anxiety": +0.20,
    },
    "short_reply": {
        "valence": -0.05, "arousal": -0.05, "loneliness": +0.05,
        "anxiety": +0.10,
    },
    "praised": {
        "valence": +0.35, "arousal": +0.20, "loneliness": -0.20,
        "anxiety": -0.15,
    },
    "conflict": {
        "valence": -0.30, "arousal": +0.30, "loneliness": +0.10,
        "anxiety": +0.25,
    },
    "sweet_words": {
        "valence": +0.50, "arousal": +0.25, "loneliness": -0.40,
        "anxiety": -0.25,
    },
    "silence_12h": {
        "valence": -0.05, "arousal": -0.10, "loneliness": +0.25,
        "anxiety": +0.10,
    },
    "silence_24h": {
        "valence": -0.10, "arousal": -0.15, "loneliness": +0.40,
        "anxiety": +0.20,
    },
}

# Telos trigger thresholds. Tuning these directly changes how chatty kanae is.
# 2026-05-13:
#   - affection_expression_affection raised 0.8→0.9 so baseline (0.85)
#     doesn't trip the motivation every tick.
#   - affection_expression_satisfaction → affection_expression_valence
#     after satisfaction was removed; "high affection + good mood" is the
#     intent and valence captures it.
TELOS_THRESHOLDS: dict[str, float] = {
    "loneliness_overflow": 0.6,
    "share_joy_valence": 0.7,
    "share_joy_arousal": 0.5,
    "anxiety_relief_anxiety": 0.5,
    "anxiety_relief_hours_silent": 8.0,
    "affection_expression_affection": 0.9,
    "affection_expression_valence": 0.5,
    "curiosity_drive": 0.6,
    "reconciliation_valence_neg": -0.2,
    "reconciliation_affection": 0.5,
}

# Suppression parameters.
TSUNDERE_GATE = 0.5
MAX_CONSECUTIVE_SELF_INITIATED = 2
# Quiet-hour range as [start_hour, end_hour) in JST. The user opted out of
# time-of-day suppression: with (0, 0) the half-open range matches no hour,
# so QUIET_HOUR_RANGE[0] <= now.hour < QUIET_HOUR_RANGE[1] is always False.
QUIET_HOUR_RANGE: tuple[int, int] = (0, 0)
# Setting LATE_NIGHT_HOUR_START to 24 means `now.hour >= 24` is always
# False, disabling the soft 60%-suppression block in telos.should_suppress.
LATE_NIGHT_HOUR_START = 24

# ±NOISE_AMPLITUDE random perturbation on event deltas to keep transitions
# from being too mechanical. apply_event uses random.uniform(-x, +x).
NOISE_AMPLITUDE = 0.05
