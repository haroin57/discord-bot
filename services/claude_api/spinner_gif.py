"""Option B: render the whole status line as one animated bold-shimmer GIF.

Why one image (word + meta together) rather than text-content + image-attachment:
Discord always places an attachment on its own line BELOW the message content,
and shows an image at its native pixel size — so a separate word-GIF looked
oversized and line-broke from the "(Ns · tokens)" text. Baking the entire line
into a single transparent strip makes it one block = one line, at a font size
matched to Discord's ~16px message text.

The bold window sweeps the gerund word ping-pong (left→right→left) so the GIF
LOOPS SEAMLESSLY — the client plays it forever with zero edits, so the bot can
refresh (new counters / word) only every few seconds without ever stalling the
animation or hitting Discord's ~1 edit/sec bucket.

Fonts: the word uses DejaVu Sans Mono (monospace → a bold glyph occupies the
same cell as its regular form, so the sweep never shifts the layout / jitters,
and it reads CLI-ish). The meta uses Noto Sans CJK JP so "/abort で中断" renders.
"""
from __future__ import annotations

import os
import tempfile

from PIL import Image, ImageDraw, ImageFont

from services.claude_api.progress import _BOLD_WIDTH

_MONO_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_MONO_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# Match Discord's desktop message text (~16px). Tunable via env.
_FONT_PX = int(os.environ.get("SPINNER_GIF_FONT_PX", "16"))
_PAD = 4
_GAP = 8                       # space between word and meta
_ACCENT = (88, 101, 242)       # blurple — legible on both light & dark themes
_DIM = 150                     # alpha of non-highlighted word glyphs
_META_RGB = (150, 154, 165)    # muted gray for the meta, readable on both themes
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "kanae_spinner_gifs")

_reg = ImageFont.truetype(_MONO_REG, _FONT_PX)
_bold = ImageFont.truetype(_MONO_BOLD, _FONT_PX)
_meta_font = ImageFont.truetype(_CJK, _FONT_PX)
_CELL = int(round(_reg.getlength("M")))  # monospace advance (all glyphs equal)

# Baseline-aligned canvas geometry across both fonts.
_a1, _d1 = _reg.getmetrics()
_a2, _d2 = _meta_font.getmetrics()
_ASCENT = max(_a1, _a2)
_DESCENT = max(_d1, _d2)
_HEIGHT = _ASCENT + _DESCENT + 2 * _PAD
_BASELINE = _PAD + _ASCENT


def _sweep_positions(word: str) -> list[int]:
    """Forward bold-window positions (left→right). The GIF is re-uploaded each
    second, so it restarts at pos 0 anyway — no need to ping-pong the loop."""
    last = max(1, len(word) - _BOLD_WIDTH + 1)
    return list(range(last))


def _draw_frame(word: str, meta: str, pos: int) -> Image.Image:
    glyphs = word + "…"
    word_w = _CELL * len(glyphs)
    meta_w = int(round(_meta_font.getlength(meta)))
    width = _PAD * 2 + word_w + _GAP + meta_w
    im = Image.new("RGBA", (width, _HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, ch in enumerate(glyphs):
        x = _PAD + i * _CELL
        in_window = pos <= i < pos + _BOLD_WIDTH and i < len(word)
        font = _bold if in_window else _reg
        alpha = 255 if in_window else _DIM
        d.text((x, _BASELINE), ch, font=font, fill=(*_ACCENT, alpha), anchor="ls")
    d.text((_PAD + word_w + _GAP, _BASELINE), meta, font=_meta_font,
           fill=(*_META_RGB, 255), anchor="ls")
    return im


def _to_palette(im: Image.Image) -> Image.Image:
    alpha = im.split()[3]
    p = im.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255)
    mask = alpha.point(lambda a: 255 if a <= 128 else 0)
    p.paste(255, mask)
    return p


def render_status_gif(word: str, meta: str, out_path: str) -> str:
    """Render the full status line (shimmering ``word`` + static ``meta``) to a
    seamlessly-looping GIF at ``out_path`` and return it."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    frames = [_to_palette(_draw_frame(word, meta, p)) for p in _sweep_positions(word)]
    # Stretch one forward sweep to ~fill the 1s refresh window so each second
    # shows a complete sweep (clamped so very short/long words stay sane).
    dur = max(45, min(140, int(900 / len(frames))))
    frames[0].save(
        out_path, format="GIF", save_all=True, append_images=frames[1:],
        duration=dur, loop=0, transparency=255, disposal=2, optimize=False,
    )
    return out_path


if __name__ == "__main__":
    out = render_status_gif(
        "Pondering", "(12s · ↑ 1.2k tokens · /abort で中断)",
        os.path.join(_CACHE_DIR, "sample.gif"),
    )
    print("gif:", out, os.path.getsize(out), "bytes")
    # montage of frames for inspection
    word, meta = "Pondering", "(12s · ↑ 1.2k tokens · /abort で中断)"
    fr = [_draw_frame(word, meta, p) for p in _sweep_positions(word)]
    mont = Image.new("RGBA", (fr[0].width, _HEIGHT * len(fr)), (54, 57, 63, 255))
    for i, f in enumerate(fr):
        mont.alpha_composite(f, (0, i * _HEIGHT))
    png = "/tmp/discord_bot_attachments/status_gif_montage.png"
    os.makedirs(os.path.dirname(png), exist_ok=True)
    mont.convert("RGB").save(png)
    print("montage:", png, f"({fr[0].width}x{_HEIGHT} per frame)")
