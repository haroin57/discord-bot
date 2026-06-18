"""Music analysis service — Gemini 2.5 Flash primary, GPT-4o-audio fallback."""

from services.music_analyzer.analyzer import analyze_music_attachments, is_audio_attachment

__all__ = ["analyze_music_attachments", "is_audio_attachment"]
