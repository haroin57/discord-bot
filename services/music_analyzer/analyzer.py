"""Analyze music audio via multimodal LLM (Gemini primary, OpenAI fallback).

Architecture:
    audio file on disk
      -> base64 encode
      -> Gemini 2.5 Flash (inline_data) OR GPT-4o-audio-preview (input_audio)
      -> structured music analysis text
      -> injected into Claude prompt as <music_analysis> context
"""

import asyncio
import base64
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio detection
# ---------------------------------------------------------------------------
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac",
    ".wma", ".opus", ".webm",
})

_MIME_MAP = {
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/flac": "audio/flac",
    "audio/mp4": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/aac",
    "audio/ogg": "audio/ogg",
    "audio/webm": "audio/webm",
    "audio/opus": "audio/opus",
    "audio/x-ms-wma": "audio/x-ms-wma",
}

_EXT_TO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac",
    ".opus": "audio/opus", ".webm": "audio/webm", ".wma": "audio/x-ms-wma",
}

# OpenAI input_audio format field
_EXT_TO_OAI_FMT = {
    ".mp3": "mp3", ".wav": "wav", ".webm": "webm", ".ogg": "ogg",
    ".flac": "flac", ".m4a": "mp4",
}


def is_audio_attachment(meta: dict) -> bool:
    """Return True if the attachment metadata looks like an audio file."""
    mime = (meta.get("mime") or "").lower()
    ext = os.path.splitext(meta.get("filename", ""))[1].lower()
    return mime.startswith("audio/") or ext in AUDIO_EXTENSIONS


def _resolve_mime(meta: dict) -> str:
    mime = (meta.get("mime") or "").lower()
    if mime in _MIME_MAP:
        return _MIME_MAP[mime]
    ext = os.path.splitext(meta.get("filename", ""))[1].lower()
    return _EXT_TO_MIME.get(ext, "audio/mpeg")


# ---------------------------------------------------------------------------
# Analysis prompt
# ---------------------------------------------------------------------------
MUSIC_ANALYSIS_PROMPT = (
    "You are an expert in Music Information Retrieval (MIR). "
    "Listen carefully to the attached audio file and analyze it.\n\n"
    "Provide your analysis in the following format:\n\n"
    "1. **Genre/Subgenre** - Main genre and subgenre "
    "(e.g. Electronic > Drum and Bass > Liquid)\n"
    "2. **Mood/Emotion** - Multiple keywords describing the atmosphere "
    "(e.g. euphoric, nostalgic, driving)\n"
    "3. **BPM** - Estimated tempo\n"
    "4. **Key** - Musical key (e.g. F# minor)\n"
    "5. **Instruments/Timbres** - Instruments, synths, and samples detected\n"
    "6. **Song Structure** - Section layout with approximate timestamps "
    "(Intro -> Verse -> Chorus etc.)\n"
    "7. **Rhythm Pattern** - Beat characteristics "
    "(four-on-the-floor, shuffle, breakbeat, etc.)\n"
    "8. **Acoustic Characteristics** - Loudness, spatial width, "
    "frequency balance impressions\n"
    "9. **Similar Artists** - 2-3 artists or tracks with a similar vibe\n"
    "10. **Overall Impression** - General impression, standout elements, "
    "and highlights\n\n"
    "Respond in Japanese. Use technical terms where appropriate."
)

# ---------------------------------------------------------------------------
# Timeouts & URLs
# ---------------------------------------------------------------------------
_GEMINI_TIMEOUT = aiohttp.ClientTimeout(total=180)
_OPENAI_TIMEOUT = aiohttp.ClientTimeout(total=180)
_OVERALL_TIMEOUT_S = 500  # per-file wall-clock cap

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def analyze_music_attachments(
    arbitrary_attachments: list[dict],
) -> str | None:
    """Analyze audio files among arbitrary attachments.

    Returns a formatted analysis string or None if no audio found / all failed.
    """
    audio_files = [a for a in arbitrary_attachments if is_audio_attachment(a)]
    if not audio_files:
        return None

    results: list[str] = []
    for af in audio_files[:3]:
        try:
            analysis = await asyncio.wait_for(
                _analyze_one(af), timeout=_OVERALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning("Music analysis timed out for %s", af.get("filename"))
            analysis = None
        if analysis:
            results.append(f"### {af['filename']}\n{analysis}")

    if not results:
        return None
    return "\n\n---\n\n".join(results)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------
async def _analyze_one(meta: dict) -> str | None:
    path = meta["path"]
    if not os.path.isfile(path):
        log.warning("Music analyzer: file not found: %s", path)
        return None

    from config import GOOGLE_AI_API_KEY, OPENAI_API_KEY

    # Gemini primary
    if GOOGLE_AI_API_KEY:
        try:
            result = await _gemini(path, meta)
            if result:
                log.info(
                    "Music analysis via Gemini OK for %s (%d chars)",
                    meta["filename"], len(result),
                )
                return result
        except Exception:
            log.warning(
                "Gemini music analysis failed for %s",
                meta["filename"], exc_info=True,
            )

    # OpenAI fallback
    if OPENAI_API_KEY:
        try:
            result = await _openai(path, meta)
            if result:
                log.info(
                    "Music analysis via OpenAI fallback OK for %s (%d chars)",
                    meta["filename"], len(result),
                )
                return result
        except Exception:
            log.warning(
                "OpenAI music analysis fallback failed for %s",
                meta["filename"], exc_info=True,
            )

    return None


def _read_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


async def _gemini(path: str, meta: dict) -> str | None:
    from config import GOOGLE_AI_API_KEY

    mime = _resolve_mime(meta)
    audio_b64 = await asyncio.to_thread(_read_b64, path)

    url = _GEMINI_URL.format(model="gemini-2.5-flash", key=GOOGLE_AI_API_KEY)
    body = {
        "contents": [{
            "parts": [
                {"text": MUSIC_ANALYSIS_PROMPT},
                {"inline_data": {"mime_type": mime, "data": audio_b64}},
            ],
        }],
        "generationConfig": {"maxOutputTokens": 4096},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, timeout=_GEMINI_TIMEOUT) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning("Gemini %d: %s", resp.status, text[:300])
                return None
            data = await resp.json()

    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "\n".join(p["text"] for p in parts if "text" in p) or None
    except (KeyError, IndexError):
        log.warning("Gemini response parse error: %s", str(data)[:300])
        return None


async def _openai(path: str, meta: dict) -> str | None:
    from config import OPENAI_API_KEY

    ext = os.path.splitext(meta.get("filename", ""))[1].lower()
    fmt = _EXT_TO_OAI_FMT.get(ext, "mp3")
    audio_b64 = await asyncio.to_thread(_read_b64, path)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "gpt-4o-audio-preview",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": MUSIC_ANALYSIS_PROMPT},
                {
                    "type": "input_audio",
                    "input_audio": {"data": audio_b64, "format": fmt},
                },
            ],
        }],
        "max_tokens": 4096,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _OPENAI_URL, headers=headers, json=body, timeout=_OPENAI_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning("OpenAI audio %d: %s", resp.status, text[:300])
                return None
            data = await resp.json()

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        log.warning("OpenAI response parse error: %s", str(data)[:300])
        return None
