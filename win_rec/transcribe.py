from __future__ import annotations

import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

from pydub import AudioSegment
from pydub.silence import detect_silence
import pydub.utils

from . import config

# Tell pydub where ffmpeg/ffprobe are (critical when running as a frozen exe)
pydub.utils.get_player_name = lambda: config.ffmpeg_path()
AudioSegment.converter = config.ffmpeg_path()
AudioSegment.ffmpeg = config.ffmpeg_path()
AudioSegment.ffprobe = config.ffprobe_path()


@dataclass
class Segment:
    speaker: str
    start: float
    end: float
    text: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        ts = _fmt_timestamp(self.start)
        return f"**[{ts}] {self.speaker}:** {self.text}"


def _fmt_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _split_audio(audio_path: Path) -> list[tuple[float, AudioSegment]]:
    """Split audio into chunks suitable for the transcription API.

    Returns list of (start_offset_seconds, chunk) tuples. Chunks aim to break
    on silences; if a stretch with no silence exceeds MAX_CHUNK_MS, hard-cut.
    Adjacent chunks overlap by CHUNK_OVERLAP_MS so words near boundaries aren't lost.
    """
    audio = AudioSegment.from_file(audio_path)
    if len(audio) <= config.MAX_CHUNK_MS:
        return [(0.0, audio)]

    silences = detect_silence(audio, min_silence_len=700, silence_thresh=-40)
    cut_points = [s[0] + (s[1] - s[0]) // 2 for s in silences]
    cut_points = [c for c in cut_points if 1000 < c < len(audio) - 1000]

    chunks: list[tuple[float, AudioSegment]] = []
    start_ms = 0
    while start_ms < len(audio):
        max_end = min(start_ms + config.MAX_CHUNK_MS, len(audio))
        candidates = [c for c in cut_points if start_ms + 30_000 < c <= max_end]
        end_ms = candidates[-1] if candidates else max_end
        chunk_start = max(0, start_ms - config.CHUNK_OVERLAP_MS) if start_ms > 0 else 0
        chunk = audio[chunk_start:end_ms]
        chunks.append((chunk_start / 1000.0, chunk))
        start_ms = end_ms
    return chunks


def _openai_client():
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    from openai import OpenAI
    kwargs = {"api_key": config.OPENAI_API_KEY}
    if config.OPENAI_BASE_URL:
        kwargs["base_url"] = config.OPENAI_BASE_URL
    return OpenAI(**kwargs)


def _pick_backend() -> str:
    explicit = config.TRANSCRIBE_BACKEND
    if explicit in {"local", "faster_whisper"}:
        return "local"
    if explicit in {"openai", "api"}:
        return "openai"
    try:
        import faster_whisper  # noqa: F401
        return "local"
    except ImportError:
        pass
    if config.OPENAI_API_KEY:
        return "openai"
    raise RuntimeError(
        "No transcription backend available: install `faster-whisper` "
        "(pip install faster-whisper) for local transcription, "
        "or set OPENAI_API_KEY for an OpenAI-compatible service."
    )


def transcribe_file(audio_path: Path, *, speaker_label: str) -> list[Segment]:
    """Transcribe an entire audio file, returning timestamped segments labelled with `speaker_label`."""
    backend = _pick_backend()
    if backend == "local":
        return _transcribe_local(audio_path, speaker_label)
    return _transcribe_openai(audio_path, speaker_label)


def _transcribe_local(audio_path: Path, speaker_label: str) -> list[Segment]:
    """VAD-slice + Whisper transcribe with incremental checkpointing.

    Every VAD range's result is appended to `<audio_stem>.partial.jsonl` as it
    completes, so a mid-transcription crash can resume from where it left off
    on the next run instead of losing everything.

    On successful completion the partial file is removed.
    """
    speech_ranges = _vad_speech_ranges(audio_path) if config.VAD_ENABLED else None
    if speech_ranges is None:
        # No VAD path — no natural checkpoint boundaries, single big call.
        return _whisper_pass(audio_path, speaker_label, offset_s=0.0)

    if not speech_ranges:
        return []

    partial_path = audio_path.parent / f"{audio_path.stem}.partial.jsonl"

    # Resume: load any previously-completed VAD ranges from partial jsonl
    import json as _json
    completed: dict[int, list[Segment]] = {}
    if partial_path.exists():
        try:
            with partial_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except _json.JSONDecodeError:
                        break
                    idx = rec.get("vad_index")
                    if not isinstance(idx, int):
                        continue
                    segs_data = rec.get("segments") or []
                    completed[idx] = [Segment(**s) for s in segs_data]
        except OSError:
            completed = {}
        if completed:
            import sys
            print(
                f"[resume] {audio_path.name}: cached {len(completed)}/{len(speech_ranges)} "
                f"VAD range(s) — will skip and continue",
                file=sys.stderr,
            )

    audio = AudioSegment.from_file(audio_path)
    all_segments: list[Segment] = []
    skipped_quiet = 0
    partial_fh = partial_path.open("a", encoding="utf-8")

    try:
        for vad_index, (start_ms, end_ms) in enumerate(speech_ranges):
            if vad_index in completed:
                all_segments.extend(completed[vad_index])
                continue

            clip = audio[start_ms:end_ms]

            try:
                level_db = clip.dBFS
            except Exception:
                level_db = 0.0

            segs: list[Segment] = []
            if level_db < config.RMS_QUIET_DB:
                skipped_quiet += 1
            else:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp = Path(f.name)
                    clip.export(f, format="wav", parameters=["-ac", "1", "-ar", "16000"])
                try:
                    segs = _whisper_pass(tmp, speaker_label, offset_s=start_ms / 1000.0,
                                         source_override=audio_path.stem)
                finally:
                    tmp.unlink(missing_ok=True)

            record = {
                "vad_index": vad_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "skipped_quiet": level_db < config.RMS_QUIET_DB,
                "segments": [s.to_dict() for s in segs],
            }
            partial_fh.write(_json.dumps(record, ensure_ascii=False) + "\n")
            partial_fh.flush()
            import os as _os
            _os.fsync(partial_fh.fileno())

            all_segments.extend(segs)
    finally:
        partial_fh.close()

    try:
        partial_path.unlink()
    except FileNotFoundError:
        pass

    if skipped_quiet:
        import sys
        print(
            f"[info] {audio_path.name}: skipped {skipped_quiet}/{len(speech_ranges)} "
            f"quiet clip(s) (< {config.RMS_QUIET_DB} dBFS)",
            file=sys.stderr,
        )
    return all_segments


def _whisper_pass(audio_path: Path, speaker_label: str,
                  offset_s: float, source_override: str | None = None) -> list[Segment]:
    from faster_whisper import WhisperModel

    model = WhisperModel(config.LOCAL_WHISPER_MODEL, device="auto", compute_type="auto")

    kwargs = {
        "condition_on_previous_text": False,
        "temperature": 0.0,
    }
    if config.WHISPER_LANGUAGE:
        kwargs["language"] = config.WHISPER_LANGUAGE
    if config.WHISPER_PROMPT:
        kwargs["initial_prompt"] = config.WHISPER_PROMPT

    segments_iter, _info = model.transcribe(str(audio_path), **kwargs)
    source = source_override or audio_path.stem

    segments: list[Segment] = []
    for s in segments_iter:
        text = (s.text or "").strip()
        if not text:
            continue
        no_speech = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
        if no_speech > config.WHISPER_NO_SPEECH_MAX:
            continue
        segments.append(Segment(
            speaker=speaker_label,
            start=offset_s + float(s.start),
            end=offset_s + float(s.end),
            text=text,
            source=source,
        ))
    return segments


def _vad_speech_ranges(audio_path: Path) -> list[tuple[int, int]] | None:
    """Return list of (start_ms, end_ms) speech regions, or None if VAD unavailable/fails."""
    try:
        from silero_vad import load_silero_vad, get_speech_timestamps, read_audio
    except ImportError as e:
        import sys
        print(
            f"[warn] silero-vad not importable ({e}); falling back to whole-file whisper "
            f"(more hallucinations possible). Install with: pip install silero-vad",
            file=sys.stderr,
        )
        return None

    try:
        model = load_silero_vad()
        wav = read_audio(str(audio_path), sampling_rate=16000)
        ts = get_speech_timestamps(
            wav, model,
            sampling_rate=16000,
            min_speech_duration_ms=500,
            min_silence_duration_ms=800,
            speech_pad_ms=150,
            threshold=0.55,
            return_seconds=False,
        )
    except Exception as e:
        import sys
        print(
            f"[warn] silero-vad crashed on {audio_path.name}: {e}; "
            f"falling back to whole-file whisper (more hallucinations possible)",
            file=sys.stderr,
        )
        return None

    return [(int(t["start"] * 1000 / 16000), int(t["end"] * 1000 / 16000)) for t in ts]


def _transcribe_openai(audio_path: Path, speaker_label: str) -> list[Segment]:
    client = _openai_client()
    chunks = _split_audio(audio_path)
    all_segments: list[Segment] = []

    for offset_s, chunk in chunks:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            tmp_path = Path(f.name)
            chunk.export(f, format="mp4", codec="aac", bitrate="32k",
                         parameters=["-ac", "1", "-ar", "16000"])

        try:
            with tmp_path.open("rb") as fh:
                result = client.audio.transcriptions.create(
                    model=config.TRANSCRIBE_MODEL,
                    file=fh,
                    response_format="verbose_json",
                )
        finally:
            tmp_path.unlink(missing_ok=True)

        chunk_segments = _result_to_segments(result, offset_s, speaker_label, source=audio_path.stem)
        all_segments.extend(chunk_segments)

    return _dedupe_overlap(all_segments)


def _result_to_segments(result, offset_s: float, speaker: str, source: str) -> list[Segment]:
    segs: list[Segment] = []
    raw = result if isinstance(result, dict) else result.model_dump()
    raw_segments = raw.get("segments") or []
    if not raw_segments:
        text = raw.get("text", "").strip()
        if text:
            duration = raw.get("duration", 0.0)
            segs.append(Segment(
                speaker=speaker,
                start=offset_s,
                end=offset_s + duration,
                text=text,
                source=source,
            ))
        return segs
    for s in raw_segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        no_speech = float(s.get("no_speech_prob") or 0.0)
        if no_speech > config.WHISPER_NO_SPEECH_MAX:
            continue
        segs.append(Segment(
            speaker=speaker,
            start=offset_s + float(s.get("start", 0.0)),
            end=offset_s + float(s.get("end", 0.0)),
            text=text,
            source=source,
        ))
    return segs


def _dedupe_overlap(segments: list[Segment]) -> list[Segment]:
    """Drop segments whose text duplicates a recent prior segment (chunk overlap)."""
    seen: list[Segment] = []
    for seg in segments:
        if seen and seg.start < seen[-1].end - 0.1 and seg.text == seen[-1].text:
            continue
        seen.append(seg)
    return seen
