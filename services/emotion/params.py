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
    # affection / curiosity も触る (2026-05-14): 旧8軸表の名残で valence/
    # arousal/loneliness/anxiety の 4 軸しか書かれていなかった。inertia
    # γ_affection=0.95, γ_curiosity=0.7 で event がなければほぼ動かないので、
    # affection/curiosity は **小さめ** に置く（valence 等の主軸の 1/3〜1/2）。
    "senpai_initiated": {
        "valence": +0.20, "arousal": +0.10, "loneliness": -0.30,
        "anxiety": -0.15, "affection": +0.05, "curiosity": +0.03,
    },
    "long_good_chat": {
        "valence": +0.40, "arousal": +0.15, "loneliness": -0.50,
        "anxiety": -0.20, "affection": +0.08, "curiosity": +0.05,
    },
    "read_ignore": {
        "valence": -0.10, "arousal": +0.05, "loneliness": +0.15,
        "anxiety": +0.20, "affection": -0.05, "curiosity": -0.02,
    },
    "short_reply": {
        # 機械的応答: 主軸だけ反応、affection/curiosity は動かさない
        "valence": -0.05, "arousal": -0.05, "loneliness": +0.05,
        "anxiety": +0.10,
    },
    "praised": {
        "valence": +0.35, "arousal": +0.20, "loneliness": -0.20,
        "anxiety": -0.15, "affection": +0.10, "curiosity": +0.03,
    },
    "conflict": {
        "valence": -0.30, "arousal": +0.30, "loneliness": +0.10,
        "anxiety": +0.25, "affection": -0.10, "curiosity": -0.02,
    },
    "sweet_words": {
        "valence": +0.50, "arousal": +0.25, "loneliness": -0.40,
        "anxiety": -0.25, "affection": +0.15, "curiosity": +0.05,
    },
    "silence_6h": {
        "valence": -0.02, "arousal": -0.05, "loneliness": +0.10,
        "anxiety": +0.05, "curiosity": +0.08,
    },
    "silence_12h": {
        "valence": -0.05, "arousal": -0.10, "loneliness": +0.25,
        "anxiety": +0.10, "affection": -0.02, "curiosity": +0.05,
    },
    "silence_24h": {
        "valence": -0.10, "arousal": -0.15, "loneliness": +0.40,
        "anxiety": +0.20, "affection": -0.05,
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

# ---------------------------------------------------------------------------
# Derived secondary emotion thresholds (used by format.derive_secondary_emotions)
# ---------------------------------------------------------------------------
# Centralized so the magic numbers that used to be sprinkled across
# format.py can be tuned in one place. Half-open ranges follow the same
# [min, max) convention as QUIET_HOUR_RANGE.
SECONDARY_EMOTION_THRESHOLDS: dict[str, float] = {
    # 拗ね気味 — affection 高 + valence ↓ + 寂しい
    "sulky_valence_max": 0.0,
    "sulky_affection_min": 0.7,
    "sulky_loneliness_min": 0.5,
    # 焼きもち — affection + anxiety 立つ、valence 伸びない
    "jealous_affection_min": 0.7,
    "jealous_anxiety_min": 0.4,
    "jealous_valence_max": 0.3,
    # 甘えたい — affection 強 + 中程度の寂しさ + 不安低
    "spoiled_affection_min": 0.8,
    "spoiled_loneliness_min": 0.4,
    "spoiled_loneliness_max": 0.7,
    "spoiled_anxiety_max": 0.3,
    # 喜んでいる — valence 高 + arousal 高
    "joyful_valence_min": 0.5,
    "joyful_arousal_min": 0.5,
    # 照れ気味 — affection 強 + ややそわそわ + valence positive 寄り
    "shy_affection_min": 0.8,
    "shy_anxiety_min": 0.2,
    "shy_anxiety_max": 0.5,
    "shy_valence_min": 0.2,
    # 不機嫌 — valence 大きく下、arousal 立つ
    "grumpy_valence_max": -0.2,
    "grumpy_arousal_min": 0.4,
    # 満たされている — valence 高 + 寂しさも不安も低
    "fulfilled_valence_min": 0.5,
    "fulfilled_loneliness_max": 0.3,
    "fulfilled_anxiety_max": 0.2,
    # 興味津々 — curiosity 高 + arousal 伴う
    "curious_curiosity_min": 0.65,
    "curious_arousal_min": 0.4,
}

# ---------------------------------------------------------------------------
# Time-decay quality modifier blending weights
# ---------------------------------------------------------------------------
# `_quality_modifier(state)` blends the last interaction's `quality` and
# `senpai_responsiveness` into a single 0..1 multiplier. The old
# implementation used the product q*r, which conflated two
# qualitatively different signals (a warm but unresponsive senpai vs.
# a terse but responsive one collapsed to the same value). Weighted
# average preserves the asymmetry:
#   - DECAY_QUALITY_WEIGHT       → weight of conversation depth itself
#   - DECAY_RESPONSIVENESS_WEIGHT → weight of senpai's reply latency / care
# Tune by raising the responsiveness weight if "先輩がちゃんと反応してくれた"
# matters more to the decay than topic quality.
DECAY_QUALITY_WEIGHT = 0.6
DECAY_RESPONSIVENESS_WEIGHT = 0.4
assert abs(DECAY_QUALITY_WEIGHT + DECAY_RESPONSIVENESS_WEIGHT - 1.0) < 1e-9

# ---------------------------------------------------------------------------
# LLM Emotion Engine parameters (2026-06-14 transition plan)
# ---------------------------------------------------------------------------
# Model choices for Claude CLI
LLM_TRANSITION_MODEL = "haiku"    # emotion engine
DAILY_MODEL = "sonnet"            # daily life simulator

# Daily journal
DAILY_BUFFER_SIZE = 96            # ~24h at 15min intervals
DAILY_CONTEXT_COUNT = 8           # recent entries in prompt
DAILY_SUMMARY_MAX_TOKENS = 500    # incremental summary target

# CLI timeouts (seconds)
CLI_TIMEOUT = 90                  # daily (Sonnet, heavier)
CLI_TIMEOUT_EMOTION = 60          # emotion (Haiku, lighter)
CLI_TIMEOUT_SUMMARY = 30          # summary (Haiku, small)

# Deprecated (kept for fallback): LONELINESS_RATE_PER_HOUR,
# ANXIETY_RATE_PER_HOUR, EVENT_DELTAS, INERTIA event blending,
# DECAY_QUALITY_* modifiers — all superseded by LLM-based transition
