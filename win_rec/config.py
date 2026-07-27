from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("AI_REC_DATA", Path.home() / "AI_Rec_Data"))
RECORDINGS_DIR = DATA_ROOT / "recordings"
BACKUP_DIR = DATA_ROOT / "Backup"        # Excluded from list_sessions / run-daily
RUNTIME_DIR = DATA_ROOT / ".runtime"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL")

TRANSCRIBE_MODEL = os.environ.get("AI_REC_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
TRANSCRIBE_BACKEND = os.environ.get("AI_REC_TRANSCRIBE_BACKEND", "auto").lower()
LOCAL_WHISPER_MODEL = os.environ.get("AI_REC_LOCAL_WHISPER_MODEL", "large-v3")
WHISPER_PROMPT = os.environ.get("AI_REC_WHISPER_PROMPT", "") or None
WHISPER_LANGUAGE = os.environ.get("AI_REC_WHISPER_LANGUAGE") or None
VAD_ENABLED = os.environ.get("AI_REC_VAD", "1").lower() not in {"0", "false", "no"}

# Quiet-clip filter: VAD may pick up faint background noise (breathing, keyboard,
# ambient) as speech. If a clip's average level is below this dBFS floor, skip
# Whisper entirely for it. -45 dBFS is well below normal speech (~-20 to -10)
# but above true silence (~-70). Set to a very negative number to disable.
RMS_QUIET_DB = float(os.environ.get("AI_REC_RMS_QUIET_DB", "-45"))

# Whisper self-report: each transcribed segment has a `no_speech_prob` (0..1).
# If above this ceiling, discard the segment (was likely hallucinated on noise).
# Set to 1.0 to disable.
WHISPER_NO_SPEECH_MAX = float(os.environ.get("AI_REC_WHISPER_NO_SPEECH_MAX", "0.6"))
SUMMARY_MODEL = os.environ.get("AI_REC_SUMMARY_MODEL", "claude-sonnet-4-6")
SUMMARY_BACKEND = os.environ.get("AI_REC_SUMMARY_BACKEND", "auto").lower()
CLAUDE_CLI = os.environ.get("AI_REC_CLAUDE_CLI", "claude")
CLAUDE_CLI_MODEL = os.environ.get("AI_REC_CLAUDE_CLI_MODEL", "sonnet")
REFINE_ENABLED = os.environ.get("AI_REC_REFINE", "1").lower() not in {"0", "false", "no"}

MAX_CHUNK_MS = 10 * 60 * 1000
CHUNK_OVERLAP_MS = 200


def ensure_dirs() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def ffmpeg_path() -> str:
    """Return path to ffmpeg: bundled exe dir when frozen, otherwise system PATH."""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    return "ffmpeg"


def ffprobe_path() -> str:
    """Return path to ffprobe: bundled exe dir when frozen, otherwise system PATH."""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "ffprobe.exe"
        if candidate.exists():
            return str(candidate)
    fp = shutil.which("ffprobe")
    if fp:
        return fp
    return "ffprobe"
