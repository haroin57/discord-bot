"""Claude-Code-CLI-style progress spinner for the Discord status message.

Replicates the official CLI's "thinking" indicator:
  - a random present-progressive gerund ("Pondering", "Reticulating", …)
    drawn from the verb list embedded in the official `claude` binary
    (extracted 2026-06-19; 152 words);
  - a shimmer animation where a 2-character **bold** window slides one
    character at a time across the word (left→right), then the word is
    re-rolled and the sweep restarts;
  - a trailing meta line with elapsed seconds and the running output-token
    count, matching the CLI's "(Ns · ↑ X tokens · …)" tail.

NOTE on cadence: the official CLI redraws locally at ~10fps. Discord message
edits are hard-capped at ~1 edit/s sustained (a 5-edits-per-5s token bucket,
measured directly 2026-06-19), so the animator in bot.py advances this shimmer
at ~1 char/second — going faster exhausts the bucket and discord.py blocks
inside message.edit(), freezing the loop. To regain continuous motion without
edits, an *animated application emoji* (SPINNER_EMOJI, see
scripts/setup_spinner_emoji.py) is prepended: it spins client-side at full
framerate for free, while the word/elapsed/tokens still update at 1/sec.
"""

from __future__ import annotations

import os
import random

# Present-progressive verbs extracted verbatim from the official claude-code
# binary's embedded data section (Bun length-prefixed strings), 2026-06-19.
SPINNER_WORDS: tuple[str, ...] = (
    "Accomplishing", "Actioning", "Actualizing", "Architecting", "Baking",
    "Beaming", "Beboppin'", "Billowing", "Blanching", "Boogieing",
    "Boondoggling", "Booping", "Bootstrapping", "Brewing", "Bunning",
    "Burrowing", "Calculating", "Caramelizing", "Cascading", "Catapulting",
    "Cerebrating", "Channelling", "Choreographing", "Churning", "Clauding",
    "Combobulating", "Composing", "Computing", "Considering", "Contemplating",
    "Cooking", "Crafting", "Creating", "Crunching", "Crystallizing",
    "Cultivating", "Deciphering", "Deliberating", "Determining",
    "Dilly-dallying", "Doing", "Doodling", "Drizzling", "Ebbing", "Effecting",
    "Elucidating", "Embellishing", "Envisioning", "Evaporating",
    "Fiddle-faddling", "Finagling", "Flibbertigibbeting", "Flowing", "Forging",
    "Forming", "Frosting", "Gallivanting", "Galloping", "Gesticulating",
    "Germinating", "Gitifying", "Grooving", "Gusting", "Harmonizing",
    "Hashing", "Hatching", "Herding", "Honking", "Hullaballooing",
    "Hyperspacing", "Ideating", "Imagining", "Improvising", "Inferring",
    "Infusing", "Ionizing", "Jitterbugging", "Kneading", "Leavening",
    "Lollygagging", "Manifesting", "Metamorphosing", "Misting", "Moonwalking",
    "Moseying", "Mulling", "Mustering", "Musing", "Nesting", "Newspapering",
    "Noodling", "Orbiting", "Orchestrating", "Osmosing", "Perambulating",
    "Percolating", "Perusing", "Philosophising", "Photosynthesizing",
    "Pollinating", "Pondering", "Pontificating", "Pouncing", "Precipitating",
    "Proofing", "Propagating", "Puttering", "Puzzling", "Quantumizing",
    "Razzle-dazzling", "Razzmatazzing", "Recombobulating", "Reticulating",
    "Roosting", "Ruminating", "Sautéing", "Scampering", "Schlepping",
    "Scurrying", "Seasoning", "Shenaniganing", "Shimmying", "Simmering",
    "Skedaddling", "Sketching", "Smooshing", "Sock-hopping", "Spinning",
    "Sprouting", "Stewing", "Sublimating", "Swirling", "Swooping",
    "Synthesizing", "Tempering", "Thinking", "Tinkering", "Tomfoolering",
    "Topsy-turvying", "Transfiguring", "Transmuting", "Twisting", "Unfurling",
    "Unravelling", "Vibing", "Waddling", "Wandering", "Warping",
    "Whatchamacalliting", "Whirlpooling", "Whirring", "Whisking", "Wibbling",
    "Working", "Wrangling", "Zesting",
)

# Width of the bold highlight window that slides across the word.
_BOLD_WIDTH = 2


def _load_spinner_emoji() -> str:
    """Optional animated application-emoji prefix for continuous motion.

    Written by scripts/setup_spinner_emoji.py to .state/spinner_emoji.txt as
    ``<a:name:id>`` markdown. Returns "" when not configured so the spinner
    degrades gracefully to text-only (1 char/sec shimmer).
    """
    try:
        from config import STATE_DIR
        with open(os.path.join(STATE_DIR, "spinner_emoji.txt")) as f:
            return f.read().strip()
    except (OSError, ImportError):
        return ""


# Resolved once at import; "" if no emoji is configured.
SPINNER_EMOJI = _load_spinner_emoji()


def pick_word() -> str:
    return random.choice(SPINNER_WORDS)


def shimmer(word: str, pos: int, *, width: int = _BOLD_WIDTH) -> str:
    """Render ``word`` with a Discord-bold window of ``width`` chars at ``pos``.

    e.g. shimmer("Pondering", 2) -> "Po**nd**ering". ``pos`` is clamped so the
    window stays within the word; markdown specials are not expected in the
    verb list, but a backslash-free word is assumed (the list is alphabetic).
    """
    if not word:
        return word
    n = len(word)
    if pos < 0:
        pos = 0
    end = min(pos + width, n)
    if pos >= end:
        return word
    return f"{word[:pos]}**{word[pos:end]}**{word[end:]}"


def _fmt_tokens(tokens: int) -> str:
    """Match the CLI's compact token count: 1234 -> '1.2k'."""
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1000:.1f}k"


class ProgressSpinner:
    """Mutable shimmer state advanced one step per animator frame.

    `step()` slides the bold window one char right; when it runs off the end
    the word is re-rolled and the sweep restarts (matching the CLI's behaviour
    of cycling words while thinking).
    """

    def __init__(self) -> None:
        self.word = pick_word()
        self.pos = 0

    def step(self) -> None:
        self.pos += 1
        # Re-roll once the bold window has fully traversed the word (+1 so the
        # last characters get a turn highlighted before the next word).
        if self.pos > len(self.word) - _BOLD_WIDTH + 1:
            self.word = pick_word()
            self.pos = 0

    def render(
        self,
        *,
        elapsed_s: int,
        tokens: int | None = None,
        interrupt_hint: str = "",
    ) -> str:
        """Build the full status line, e.g.
        "Po**nd**ering… (12s · ↑ 1.2k tokens · /abort で中断)".
        """
        head = f"{shimmer(self.word, self.pos)}…"
        meta = [f"{int(elapsed_s)}s"]
        if tokens:
            meta.append(f"↑ {_fmt_tokens(tokens)} tokens")
        if interrupt_hint:
            meta.append(interrupt_hint)
        line = f"{head} ({' · '.join(meta)})"
        return f"{SPINNER_EMOJI} {line}" if SPINNER_EMOJI else line
