from __future__ import annotations

import difflib
from pathlib import Path

from . import config
from .transcribe import Segment


def merge_dual_track(mic_segments: list[Segment], system_segments: list[Segment]) -> list[Segment]:
    """Merge two single-speaker transcripts by timestamp into a unified timeline.
    Also drops mic-side segments that duplicate a nearby system segment (bleed-through)."""
    mic_kept = _drop_cross_track_dupes(mic_segments, system_segments)
    merged = sorted(
        [*mic_kept, *system_segments],
        key=lambda s: (s.start, s.end),
    )
    return merged


def _drop_cross_track_dupes(mic_segments: list[Segment],
                            system_segments: list[Segment],
                            *,
                            time_window_s: float = 3.0,
                            sim_threshold: float = 0.6) -> list[Segment]:
    """Remove mic segments that overlap in time with a system segment of very similar text.

    Rationale: when the user isn't wearing headphones (or is using a laptop mic near speakers),
    their microphone picks up the remote party's audio too. Whisper then transcribes the same
    utterance twice — once labelled '我', once '对方'. We keep the system-track version
    (which is cleaner) and drop the mic-track duplicate.
    """
    if not mic_segments or not system_segments:
        return mic_segments

    system_sorted = sorted(system_segments, key=lambda s: s.start)
    kept: list[Segment] = []
    j = 0
    for m in mic_segments:
        # Advance j to first system segment that could possibly overlap
        while j < len(system_sorted) and system_sorted[j].end < m.start - time_window_s:
            j += 1
        is_dup = False
        k = j
        while k < len(system_sorted) and system_sorted[k].start <= m.end + time_window_s:
            s = system_sorted[k]
            if _text_similar(m.text, s.text, sim_threshold):
                is_dup = True
                break
            k += 1
        if not is_dup:
            kept.append(m)
    return kept


def _text_similar(a: str, b: str, threshold: float) -> bool:
    """Return True if strings are close enough (ratio ≥ threshold), after light normalization."""
    if not a or not b:
        return False
    a2 = "".join(ch for ch in a if ch.isalnum())
    b2 = "".join(ch for ch in b if ch.isalnum())
    if not a2 or not b2:
        return False
    # Cheap length filter — if lengths differ by >2x, they're not the same utterance
    if max(len(a2), len(b2)) / max(1, min(len(a2), len(b2))) > 2.5:
        return False
    ratio = difflib.SequenceMatcher(None, a2, b2, autojunk=False).ratio()
    return ratio >= threshold


def transcribe_with_diarization(audio_path: Path) -> list[Segment]:
    """Single-mic in-room scenario: upload to AssemblyAI for ASR + speaker diarization."""
    if not config.ASSEMBLYAI_API_KEY:
        raise RuntimeError(
            "ASSEMBLYAI_API_KEY not set. Required for --solo-mic (in-room) mode."
        )
    import assemblyai as aai

    aai.settings.api_key = config.ASSEMBLYAI_API_KEY

    transcriber = aai.Transcriber()
    cfg = aai.TranscriptionConfig(
        language_detection=True,
        speaker_labels=True,
        punctuate=True,
        format_text=True,
    )
    transcript = transcriber.transcribe(str(audio_path), config=cfg)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")

    segments: list[Segment] = []
    utterances = transcript.utterances or []
    for u in utterances:
        speaker = f"说话人{u.speaker}" if u.speaker else "说话人?"
        segments.append(Segment(
            speaker=speaker,
            start=float(u.start) / 1000.0,
            end=float(u.end) / 1000.0,
            text=(u.text or "").strip(),
            source="mic",
        ))
    return segments
