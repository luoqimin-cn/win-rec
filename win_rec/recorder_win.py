"""Windows mic-only recorder subprocess.

Launched by recorder.py's start() as a detached background process.
Captures microphone audio via ffmpeg WASAPI, writes JSON events to
recorder.log, and polls a control.cmd file for commands.

Usage (internal):
    python recorder_win.py --session-dir <path> --control-cmd <path>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


POLL_INTERVAL = 0.1   # seconds between control file polls
SEGMENT_ROTATE_ON_RESUME = True  # start a new segment file after each resume

def _ffmpeg_exe() -> str:
    """Return path to ffmpeg: bundled exe dir when frozen, otherwise system PATH."""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"

def _ffprobe_exe() -> str:
    """Return path to ffprobe: bundled exe dir when frozen, otherwise system PATH."""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "ffprobe.exe"
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _log_event(log_path: Path, event: dict) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()


def _ffmpeg_start(session_dir: Path, seg_index: int) -> subprocess.Popen:
    """Launch ffmpeg capturing from the default WASAPI microphone."""
    seg_path = session_dir / f"mic.{seg_index:03d}.m4a"
    cmd = [
        _ffmpeg_exe(), "-y",
        "-f", "wasapi",
        "-i", "default",
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "aac",
        "-b:a", "32k",
        str(seg_path),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def _stop_ffmpeg(proc: subprocess.Popen) -> None:
    """Send 'q' to ffmpeg stdin to request graceful stop, then wait."""
    # ffmpeg reads stdin for 'q'; since we used DEVNULL we must use terminate.
    # On Windows, SIGTERM is not available; terminate() sends TerminateProcess.
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _measure_duration(path: Path) -> float:
    """Return duration in seconds via ffprobe, or 0 on error."""
    try:
        result = subprocess.run(
            [
                _ffprobe_exe(), "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--control-cmd", required=True)
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    ctrl_path = Path(args.control_cmd)
    log_path = session_dir / "recorder.log"

    # Clear any stale control file.
    if ctrl_path.exists():
        ctrl_path.unlink()

    seg_index = 1
    segments: list[dict] = []
    paused = False
    mic_muted = False
    current_proc: subprocess.Popen | None = None

    _log_event(log_path, {"event": "init"})
    _log_event(log_path, {"event": "starting"})

    # Start first segment.
    current_proc = _ffmpeg_start(session_dir, seg_index)
    seg_start_time = time.time()

    _log_event(log_path, {
        "event": "started",
        "pid": os.getpid(),
        "session_dir": str(session_dir),
        "mic_only": True,
    })

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Read and clear the control file.
            cmd = None
            if ctrl_path.exists():
                try:
                    cmd = ctrl_path.read_text().strip().upper()
                    ctrl_path.unlink()
                except OSError:
                    pass

            if cmd == "STOP":
                break

            elif cmd == "PAUSE" and not paused:
                paused = True
                _stop_ffmpeg(current_proc)
                seg_duration = _measure_duration(session_dir / f"mic.{seg_index:03d}.m4a")
                segments.append({
                    "index": seg_index,
                    "path": str(session_dir / f"mic.{seg_index:03d}.m4a"),
                    "duration": seg_duration,
                    "start_offset": seg_start_time,
                })
                current_proc = None
                _log_event(log_path, {"event": "paused"})

            elif cmd == "RESUME" and paused:
                paused = False
                seg_index += 1
                current_proc = _ffmpeg_start(session_dir, seg_index)
                seg_start_time = time.time()
                _log_event(log_path, {"event": "resumed"})

            elif cmd == "MIC_OFF" and not mic_muted:
                mic_muted = True
                if not paused and current_proc is not None:
                    _stop_ffmpeg(current_proc)
                    seg_duration = _measure_duration(session_dir / f"mic.{seg_index:03d}.m4a")
                    segments.append({
                        "index": seg_index,
                        "path": str(session_dir / f"mic.{seg_index:03d}.m4a"),
                        "duration": seg_duration,
                        "start_offset": seg_start_time,
                        "mic_muted": False,
                    })
                    current_proc = None
                _log_event(log_path, {"event": "mic_off"})

            elif cmd == "MIC_ON" and mic_muted:
                mic_muted = False
                if not paused:
                    seg_index += 1
                    current_proc = _ffmpeg_start(session_dir, seg_index)
                    seg_start_time = time.time()
                _log_event(log_path, {"event": "mic_on"})

            # Check if ffmpeg died unexpectedly.
            if current_proc is not None and current_proc.poll() is not None:
                _log_event(log_path, {
                    "event": "error",
                    "msg": f"ffmpeg exited unexpectedly with code {current_proc.returncode}",
                })
                break

    finally:
        # Stop any running ffmpeg.
        if current_proc is not None:
            _stop_ffmpeg(current_proc)
            seg_duration = _measure_duration(session_dir / f"mic.{seg_index:03d}.m4a")
            segments.append({
                "index": seg_index,
                "path": str(session_dir / f"mic.{seg_index:03d}.m4a"),
                "duration": seg_duration,
                "start_offset": seg_start_time,
            })

        total_duration = sum(s.get("duration", 0) for s in segments)
        _log_event(log_path, {
            "event": "stopped",
            "system_segments": [],
            "mic_segments": segments,
            "duration": total_duration,
        })


if __name__ == "__main__":
    main()
