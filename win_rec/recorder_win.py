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
import re
import subprocess
import sys
import time
from pathlib import Path


POLL_INTERVAL = 0.1   # seconds between control file polls
SEGMENT_ROTATE_ON_RESUME = True  # start a new segment file after each resume

def _find_bundled(name: str) -> str | None:
    exe_dir = Path(sys.executable).parent
    for candidate in [
        exe_dir / name,
        exe_dir / "_internal" / name,
        Path(getattr(sys, "_MEIPASS", "")) / name,
    ]:
        if candidate.exists():
            return str(candidate)
    return None


def _ffmpeg_exe() -> str:
    if getattr(sys, "frozen", False):
        found = _find_bundled("ffmpeg.exe")
        if found:
            return found
    return "ffmpeg"


def _ffprobe_exe() -> str:
    if getattr(sys, "frozen", False):
        found = _find_bundled("ffprobe.exe")
        if found:
            return found
    return "ffprobe"


def _log_event(log_path: Path, event: dict) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()


def _list_dshow_devices() -> tuple[list[str], str]:
    """Return (device_names, raw_stderr) from dshow device listing."""
    try:
        result = subprocess.run(
            [_ffmpeg_exe(), "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
        )
        raw = result.stderr
        lines = raw.splitlines()
        devices: list[str] = []
        in_audio = False
        for line in lines:
            if "DirectShow audio devices" in line:
                in_audio = True
                continue
            if in_audio and "DirectShow" in line and "devices" in line:
                break
            if in_audio:
                m = re.search(r'"([^"]+)"', line)
                if m:
                    name = m.group(1)
                    if "Alternative name" not in line:
                        devices.append(name)
        return devices, raw
    except Exception as e:
        return [], str(e)


def _ffmpeg_start(session_dir: Path, seg_index: int,
                  devices: list[str] | None = None) -> subprocess.Popen:
    """Launch ffmpeg capturing from the default microphone.

    Priority: dshow (named device) → dshow (audio=default) → wasapi (default).
    """
    if devices is None:
        devices, _ = _list_dshow_devices()
    mic = devices[0] if devices else None

    seg_path = session_dir / f"mic.{seg_index:03d}.m4a"

    # Strategy 1: dshow with detected device name
    if mic:
        cmd = [
            _ffmpeg_exe(), "-y",
            "-f", "dshow", "-i", f"audio={mic}",
            "-ar", "16000", "-ac", "1",
            "-c:a", "aac", "-b:a", "32k",
            str(seg_path),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc

    # Strategy 2: dshow with "audio=default" (works with some drivers)
    cmd_dshow = [
        _ffmpeg_exe(), "-y",
        "-f", "dshow", "-i", "audio=default",
        "-ar", "16000", "-ac", "1",
        "-c:a", "aac", "-b:a", "32k",
        str(seg_path),
    ]
    proc = subprocess.Popen(cmd_dshow, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    if proc.poll() is None:
        return proc  # dshow default works
    # dshow default failed — clean up and fall through to wasapi
    try:
        proc.kill()
        proc.wait()
    except Exception:
        pass

    # Strategy 3: wasapi fallback (older ffmpeg builds)
    cmd_wasapi = [
        _ffmpeg_exe(), "-y",
        "-f", "wasapi", "-i", "default",
        "-ar", "16000", "-ac", "1",
        "-c:a", "aac", "-b:a", "32k",
        str(seg_path),
    ]
    proc = subprocess.Popen(cmd_wasapi, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    # Dump dshow device listing for diagnostics
    devices, dshow_raw = _list_dshow_devices()
    _log_event(log_path, {
        "event": "dshow_devices",
        "devices": devices,
        "raw_stderr_first_2k": dshow_raw[:2000] if dshow_raw else "",
    })
    _log_event(log_path, {"event": "starting"})

    # Start first segment and verify ffmpeg didn't exit immediately.
    try:
        current_proc = _ffmpeg_start(session_dir, seg_index, devices=devices)
    except RuntimeError as e:
        _log_event(log_path, {
            "event": "error",
            "message": f"ffmpeg start failed: {e}",
            "dshow_devices": devices,
            "dshow_raw_stderr": dshow_raw[:2000],
        })
        return
    seg_start_time = time.time()
    time.sleep(0.5)
    if current_proc.poll() is not None:
        _log_event(log_path, {
            "event": "error",
            "message": f"ffmpeg exited immediately (code {current_proc.returncode}); "
                       f"check that a microphone is connected and not in use by another app. "
                       f"dshow devices found: {devices or 'none'}",
        })
        return

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
                current_proc = _ffmpeg_start(session_dir, seg_index, devices=devices)
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
                    current_proc = _ffmpeg_start(session_dir, seg_index, devices=devices)
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
    # Wrap top-level so any unhandled exception writes an error event to the
    # log before exiting — otherwise recorder.py times out with no diagnosis.
    import argparse as _ap
    _pre = _ap.ArgumentParser(add_help=False)
    _pre.add_argument("--session-dir", dest="session_dir")
    _pre.add_argument("--control-cmd", dest="control_cmd")
    _known, _ = _pre.parse_known_args()
    _log_path = Path(_known.session_dir) / "recorder.log" if _known.session_dir else None
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        if _log_path:
            try:
                _log_event(_log_path, {"event": "error", "message": f"recorder_win crashed: {exc}"})
            except Exception:
                pass
        raise
