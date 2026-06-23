"""Local capture of the spinner edit timeline under Discord-like edit rate
limiting, to diagnose the "描画が止まる (freeze)" report.

We replicate Discord's per-message edit rate limit with a token bucket
(~5 edits / 5s, the empirical PATCH-message bucket) and run three candidate
animate loops against it IN REAL TIME, recording when an edit actually LANDS
(= when the user sees a new frame). The gaps between landings are the freezes.
"""
import asyncio
import sys
import time

sys.path.insert(0, "/home/ubuntu/discord-bot")
from services.claude_api.progress import ProgressSpinner  # noqa: E402

WINDOW_S = 12.0          # how long to run each variant
SPIN_INTERVAL_S = 0.25   # current cadence (4 fps)

# --- Discord-like edit bucket: 5 requests per 5s window, per message. When
#     exhausted, edit() awaits until a slot frees (this is what discord.py does
#     internally; on an actual 429 it sleeps retry_after). ---
class EditBucket:
    def __init__(self, limit=5, per=5.0, net_latency=0.12):
        self.limit = limit
        self.per = per
        self.net_latency = net_latency
        self.hits = []  # timestamps of recent edits

    async def edit(self):
        # network round-trip
        await asyncio.sleep(self.net_latency)
        now = time.monotonic()
        self.hits = [t for t in self.hits if now - t < self.per]
        if len(self.hits) >= self.limit:
            # bucket exhausted → wait until the oldest hit ages out (discord.py
            # blocks the request here; serial callers freeze for this duration)
            wait = self.per - (now - self.hits[0])
            await asyncio.sleep(max(0.0, wait))
            now = time.monotonic()
            self.hits = [t for t in self.hits if now - t < self.per]
        self.hits.append(time.monotonic())


def fmt_timeline(landings, start):
    """landings: list of (t, content). Show inter-arrival gaps + max freeze."""
    lines = []
    prev = start
    max_gap = 0.0
    for t, content in landings:
        gap = t - prev
        max_gap = max(max_gap, gap)
        flag = "  <-- FREEZE" if gap > 0.6 else ""
        lines.append(f"  t={t - start:5.2f}s (+{gap:4.2f}s) {content[:42]!r}{flag}")
        prev = t
    return lines, max_gap, len(landings)


# --- Variant A: current SERIAL loop (await each edit, step after it lands) ---
async def run_serial(bucket):
    sp = ProgressSpinner()
    landings = []
    start = time.monotonic()
    next_frame = start
    t0 = {"start": start}
    while time.monotonic() - start < WINDOW_S:
        content = sp.render(elapsed_s=time.monotonic() - start, tokens=1234,
                            interrupt_hint="/abort で中断")
        await bucket.edit()                 # SERIAL: blocks on rate limit
        landings.append((time.monotonic(), content))
        sp.step()
        next_frame += SPIN_INTERVAL_S
        delay = next_frame - time.monotonic()
        if delay < 0:
            next_frame = time.monotonic()
            delay = 0.0
        await asyncio.sleep(delay)
    return fmt_timeline(landings, start)


# --- Variant B: frame-drop (fire-and-forget; only one pending edit) ---
async def run_framedrop(bucket):
    sp = ProgressSpinner()
    landings = []
    start = time.monotonic()
    next_frame = start
    pending = {"task": None}

    async def do_edit(content):
        await bucket.edit()
        landings.append((time.monotonic(), content))

    while time.monotonic() - start < WINDOW_S:
        sp.step()  # advance every tick regardless
        if pending["task"] is None or pending["task"].done():
            content = sp.render(elapsed_s=time.monotonic() - start, tokens=1234,
                                interrupt_hint="/abort で中断")
            pending["task"] = asyncio.create_task(do_edit(content))
        next_frame += SPIN_INTERVAL_S
        delay = next_frame - time.monotonic()
        if delay < 0:
            next_frame = time.monotonic()
            delay = 0.0
        await asyncio.sleep(delay)
    return fmt_timeline(landings, start)


# --- Variant C: serial but cadence matched to the bucket (~1 edit / sec) ---
async def run_slow_serial(bucket, interval=1.0):
    sp = ProgressSpinner()
    landings = []
    start = time.monotonic()
    next_frame = start
    while time.monotonic() - start < WINDOW_S:
        content = sp.render(elapsed_s=time.monotonic() - start, tokens=1234,
                            interrupt_hint="/abort で中断")
        await bucket.edit()
        landings.append((time.monotonic(), content))
        sp.step()
        next_frame += interval
        delay = next_frame - time.monotonic()
        if delay < 0:
            next_frame = time.monotonic()
            delay = 0.0
        await asyncio.sleep(delay)
    return fmt_timeline(landings, start)


async def main():
    for name, runner in [
        ("A. SERIAL @0.25s (current)", lambda: run_serial(EditBucket())),
        ("B. FRAME-DROP @0.25s", lambda: run_framedrop(EditBucket())),
        ("C. SERIAL @1.0s (cadence-matched)", lambda: run_slow_serial(EditBucket())),
    ]:
        lines, max_gap, n = await runner()
        print(f"\n=== {name} ===")
        print(f"visible updates in {WINDOW_S:.0f}s: {n}  |  max freeze: {max_gap:.2f}s")
        for ln in lines:
            print(ln)


asyncio.run(main())
