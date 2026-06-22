"""Create (or refresh) the animated spinner application-emoji for the bot.

Why: Discord caps single-message text edits at ~1 edit/sec (5/5s token bucket,
measured 2026-06-19), so the status spinner's word/counter can only update once
a second. An *application emoji* (owned by the bot's application, usable in any
channel/DM the bot posts to, no guild/Nitro needed) animates CLIENT-SIDE with
zero edit cost — so we render a small rotating spinner once as an animated GIF,
upload it as an application emoji, and prepend `<a:name:id>` to the status line.
The glyph spins smoothly forever; the word/elapsed/tokens still update at 1/sec.

Idempotent: reuses the existing emoji if present (unless --refresh). Writes the
`<a:name:id>` markdown to .state/spinner_emoji.txt, which progress.py reads.

Run:  .venv/bin/python scripts/setup_spinner_emoji.py [--refresh]
"""
from __future__ import annotations

import base64
import io
import math
import os
import sys

import aiohttp
import asyncio
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DISCORD_TOKEN, STATE_DIR  # noqa: E402

EMOJI_NAME = "kanae_spin"
EMOJI_FILE = os.path.join(STATE_DIR, "spinner_emoji.txt")

# --- spinner art: 12 dots on a circle, a bright head fading around, rotating
#     one step per frame. Transparent bg so it blends with the chat bubble. ---
SIZE = 64
N_DOTS = 12
DOT_R = 5
RADIUS = 24
ACCENT = (88, 101, 242)  # Discord blurple — reads on both light & dark themes


def _build_gif_bytes() -> bytes:
    frames = []
    cx = cy = SIZE / 2
    for head in range(N_DOTS):
        im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for i in range(N_DOTS):
            # distance (in steps) behind the head → brighter near head
            back = (head - i) % N_DOTS
            alpha = int(255 * (1.0 - back / N_DOTS) ** 1.4)
            alpha = max(30, alpha)
            ang = 2 * math.pi * i / N_DOTS - math.pi / 2
            x = cx + RADIUS * math.cos(ang)
            y = cy + RADIUS * math.sin(ang)
            d.ellipse(
                [x - DOT_R, y - DOT_R, x + DOT_R, y + DOT_R],
                fill=(*ACCENT, alpha),
            )
        frames.append(im)

    # RGBA → palette frames with a reserved transparent index (255).
    def to_p(im: Image.Image) -> Image.Image:
        alpha = im.split()[3]
        p = im.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255)
        mask = alpha.point(lambda a: 255 if a <= 128 else 0)
        p.paste(255, mask)
        return p

    pframes = [to_p(f) for f in frames]
    buf = io.BytesIO()
    pframes[0].save(
        buf, format="GIF", save_all=True, append_images=pframes[1:],
        duration=70, loop=0, transparency=255, disposal=2, optimize=False,
    )
    return buf.getvalue()


async def main() -> None:
    refresh = "--refresh" in sys.argv
    gif = _build_gif_bytes()
    print(f"generated spinner GIF: {len(gif)} bytes ({SIZE}x{SIZE}, {N_DOTS} frames)")
    if len(gif) > 256 * 1024:
        raise SystemExit("GIF exceeds Discord's 256KB emoji limit")

    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    base = "https://discord.com/api/v10"
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{base}/oauth2/applications/@me") as r:
            app = await r.json()
            app_id = app["id"]
        print(f"application id: {app_id}")

        ep = f"{base}/applications/{app_id}/emojis"
        async with s.get(ep) as r:
            existing = (await r.json()).get("items", [])
        match = next((e for e in existing if e["name"] == EMOJI_NAME), None)

        if match and not refresh:
            emoji_id = match["id"]
            print(f"reusing existing emoji id={emoji_id}")
        else:
            if match and refresh:
                async with s.delete(f"{ep}/{match['id']}") as r:
                    print(f"deleted old emoji ({r.status})")
            data_uri = "data:image/gif;base64," + base64.b64encode(gif).decode()
            async with s.post(ep, json={"name": EMOJI_NAME, "image": data_uri}) as r:
                if r.status not in (200, 201):
                    raise SystemExit(f"create failed {r.status}: {await r.text()}")
                created = await r.json()
                emoji_id = created["id"]
            print(f"created emoji id={emoji_id}")

        markdown = f"<a:{EMOJI_NAME}:{emoji_id}>"
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(EMOJI_FILE, "w") as f:
            f.write(markdown)
        print(f"wrote {EMOJI_FILE}: {markdown}")


if __name__ == "__main__":
    asyncio.run(main())
