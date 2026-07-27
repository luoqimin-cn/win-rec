from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, store


ACTIVE_FILE = config.RUNTIME_DIR / "active.json"


class RecorderError(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_active() -> dict | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_FILE.read_text())
    except json.JSONDecodeError:
        return None


def _write_active(info: dict) -> None:
    ACTIVE_FILE.write_text(json.dumps(info, indent=2))


def _clear_active() -> None:
    if ACTIVE_FILE.exists():
        ACTIVE_FILE.unlink()


def is_recording() -> bool:
    active = _read_active()
    if not active:
        return False
    pid = active.get("pid")
    if not pid or not _alive(pid):
        _clear_active()
        return False
    return True


def current_state() -> dict:
    """Return {'recording': str, 'mic': str} where recording ∈ {idle, recording, paused}
    and mic ∈ {on, off, n/a}."""
    active = _read_active()
    if not active:
        return {"recording": "idle", "mic": "n/a"}
    pid = active.get("pid")
    if not pid or not _alive(pid):
        return {"recording": "idle", "mic": "n/a"}
    log_path = Path(active["session_dir"]) / "recorder.log"
    rec_state = "recording"
    mic_state = "on"
    if log_path.exists():
        with log_path.open("rb") as f:
            for raw in f:
                try:
                    evt = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                e = evt.get("event")
                if e in {"started", "resumed"}:
                    rec_state = "recording"
                elif e == "paused":
                    rec_state = "paused"
                elif e == "mic_off":
                    mic_state = "off"
                elif e == "mic_on":
                    mic_state = "on"
    # When paused, mic is not active regardless of its last toggle state
    if rec_state == "paused":
        mic_state = "paused"
    return {"recording": rec_state, "mic": mic_state}


def _tail_log_for_event(log_path: Path, target_events: set[str],
                         timeout: float = 15.0, start_pos: int = 0) -> tuple[dict, int]:
    deadline = time.time() + timeout
    pos = start_pos
    while time.time() < deadline:
        if log_path.exists():
            with log_path.open("rb") as f:
                f.seek(pos)
                for raw in f:
                    pos = f.tell()
                    try:
                        evt = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if evt.get("event") in target_events:
                        return evt, pos
        time.sleep(0.1)
    raise RecorderError(
        f"timed out waiting for {target_events} in {log_path} after {timeout}s"
    )


def _log_pos(log_path: Path) -> int:
    return log_path.stat().st_size if log_path.exists() else 0


def _send_command(ctrl_path: Path, cmd: str) -> None:
    if not ctrl_path.exists():
        # On Windows, write the file (recorder polls for its existence).
        pass
    try:
        ctrl_path.write_text(cmd + "\n")
    except OSError as e:
        raise RecorderError(f"cannot write to control file: {e}") from e


def notify(title: str, message: str) -> None:
    """No-op on Windows (no osascript)."""
    pass


# ─────────────────────────────────────────────────────────────
# Zombie recovery
# ─────────────────────────────────────────────────────────────

def check_and_recover_zombie() -> tuple[str, store.Session] | None:
    active = _read_active()
    if not active:
        return None
    pid = active.get("pid")
    if pid and _alive(pid):
        return None
    session_dir = Path(active.get("session_dir", ""))
    session = store.Session(session_id=session_dir.name, dir=session_dir)

    ctrl_path = Path(active.get("ctrl", ""))
    if ctrl_path.exists():
        try:
            ctrl_path.unlink()
        except OSError:
            pass

    mic_files = sorted(session.dir.glob("mic.[0-9][0-9][0-9].m4a"))
    if not mic_files:
        _clear_active()
        return (f"cleared dead session {session.session_id} (no audio recovered)", session)

    # Best-effort concat (no silence padding in recovery)
    if len(mic_files) == 1 and not session.mic_audio.exists():
        shutil.copy2(mic_files[0], session.mic_audio)
        concat_msg = f"mic: 1 segment → mic.m4a"
    elif not session.mic_audio.exists():
        _ffmpeg_concat_copy(mic_files, session.mic_audio)
        concat_msg = f"mic: {len(mic_files)} segments concatenated → mic.m4a"
    else:
        concat_msg = "mic.m4a already exists"

    meta = session.read_meta()
    meta["stopped_at"] = time.time()
    meta["stopped_by"] = "zombie_recovery"
    meta["duration"] = _measure_m4a_seconds(session.mic_audio) or 0.0
    session.write_meta(meta)

    _clear_active()
    store.clear_current()
    return (f"recovered zombie session {session.session_id}: {concat_msg}", session)


def _measure_m4a_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        from mutagen.mp4 import MP4
        return float(MP4(str(path)).info.length)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# ffmpeg concat helpers
# ─────────────────────────────────────────────────────────────

def _ensure_ffmpeg() -> str:
    ff = config.ffmpeg_path()
    try:
        subprocess.run([ff, "-version"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise RecorderError(
            "ffmpeg not found — required for multi-segment recordings. "
            "Install ffmpeg and ensure it is in PATH."
        )
    return ff


def _ffmpeg_concat_copy(files: list[Path], output: Path) -> None:
    """Concatenate m4a files with no re-encoding (fast, seconds).
    All inputs must have identical codec params."""
    ff = _ensure_ffmpeg()
    list_file = output.parent / f".concat_{output.stem}.txt"
    list_file.write_text("\n".join(f"file '{p}'" for p in files) + "\n")
    try:
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0",
             "-i", str(list_file),
             "-c", "copy",
             str(output)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RecorderError(f"ffmpeg concat failed: {r.stderr[:400]}")
    finally:
        list_file.unlink(missing_ok=True)


def _ffmpeg_gen_silence(session_dir: Path, duration_sec: float, tag: str) -> Path:
    """Generate a silent m4a matching the recorder's AAC/16k/mono/32k format."""
    ff = _ensure_ffmpeg()
    path = session_dir / f".silence_{tag}.m4a"
    r = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
         "-t", f"{max(0.05, duration_sec):.3f}",
         "-c:a", "aac", "-b:a", "32k",
         str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RecorderError(f"ffmpeg silence generation failed: {r.stderr[:400]}")
    return path


def _concat_mic_with_silence(session: store.Session,
                              mic_segs: list[dict],
                              target_duration: float) -> str:
    """Concat mic segments into session.mic_audio.

    In mic-only mode there is no system reference track, so we simply
    concatenate all segments without silence padding. On PAUSE, recorder_win
    stops writing, so the gap is already absent from the source.

    Fast path (no re-encode) when there is a single unbroken segment.
    """
    if not mic_segs:
        if target_duration > 0:
            silence = _ffmpeg_gen_silence(session.dir, target_duration, "all")
            shutil.move(str(silence), str(session.mic_audio))
            return f"mic: no segments, filled with {target_duration:.1f}s silence"
        return "mic: nothing to concat"

    seg_files = [Path(s["path"]) for s in mic_segs]

    if len(seg_files) == 1:
        shutil.copy2(seg_files[0], session.mic_audio)
        return "mic: 1 segment → mic.m4a (no re-encode)"

    _ffmpeg_concat_copy(seg_files, session.mic_audio)
    return f"mic: {len(seg_files)} segments concatenated → mic.m4a"


def _ffmpeg_concat_filter(parts: list[tuple[str, object]], output: Path) -> None:
    """Concat a heterogeneous sequence of ('segment', file_path) and
    ('silence', duration_seconds) entries using ffmpeg's concat filter."""
    ff = _ensure_ffmpeg()
    input_args: list[str] = []
    filter_pieces: list[str] = []
    for i, (kind, val) in enumerate(parts):
        if kind == "silence":
            dur = max(0.05, float(val))
            input_args.extend([
                "-f", "lavfi",
                "-t", f"{dur:.3f}",
                "-i", "anullsrc=r=16000:cl=mono",
            ])
        else:
            input_args.extend(["-i", str(val)])
        filter_pieces.append(f"[{i}:a]")
    filter_complex = "".join(filter_pieces) + f"concat=n={len(parts)}:v=0:a=1[out]"
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"] + input_args + [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "32k", "-ar", "16000", "-ac", "1",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RecorderError(f"ffmpeg concat filter failed: {r.stderr[:400]}")


# ─────────────────────────────────────────────────────────────
# Public commands
# ─────────────────────────────────────────────────────────────

def start(name: str | None = None) -> store.Session:
    recovery = check_and_recover_zombie()
    if recovery:
        print(f"[recovery] {recovery[0]}", file=sys.stderr)

    if is_recording():
        raise RecorderError("a recording is already in progress; run `win-rec stop` first")

    config.ensure_dirs()
    session = store.new_session(name=name)

    ctrl_path = session.dir / "control.cmd"

    if getattr(sys, "frozen", False):
        recorder_exe = Path(sys.executable).parent / "win-rec-recorder.exe"
        cmd = [str(recorder_exe), "--session-dir", str(session.dir), "--control-cmd", str(ctrl_path)]
    else:
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "recorder_win.py"),
            "--session-dir", str(session.dir),
            "--control-cmd", str(ctrl_path),
        ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    log_path = session.dir / "recorder.log"
    try:
        evt, _ = _tail_log_for_event(log_path, {"started", "error"}, timeout=15.0)
    except RecorderError:
        exit_code = proc.poll()
        proc.terminate()
        if exit_code is not None:
            raise RecorderError(
                f"recorder subprocess exited immediately (code {exit_code}); "
                "check that win-rec-recorder.exe is present and not blocked by antivirus"
            )
        raise

    if evt.get("event") == "error":
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        raise RecorderError(f"recorder failed to start: {evt.get('message')}")

    meta = {
        "session_id": session.session_id,
        "name": name,
        "started_at": time.time(),
        "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mic_segments": [],
        "pauses": [],
        "mic_intervals": [],
    }
    session.write_meta(meta)
    store.set_current(session)

    _write_active({
        "pid": proc.pid,
        "session_id": session.session_id,
        "session_dir": str(session.dir),
        "ctrl": str(ctrl_path),
    })
    return session


def pause() -> store.Session:
    active = _read_active()
    if not active:
        raise RecorderError("no active recording")
    if not _alive(active["pid"]):
        _clear_active()
        raise RecorderError("recorder process is gone")

    session = store.Session(session_id=Path(active["session_dir"]).name,
                            dir=Path(active["session_dir"]))
    log_path = session.dir / "recorder.log"
    pos = _log_pos(log_path)
    _send_command(Path(active["ctrl"]), "PAUSE")
    evt, _ = _tail_log_for_event(log_path, {"paused", "error"}, timeout=15.0, start_pos=pos)
    if evt.get("event") == "error":
        raise RecorderError(f"pause failed: {evt.get('message')}")

    def _mutate(meta: dict) -> None:
        meta.setdefault("pauses", []).append({"paused_at": time.time()})
    session.update_meta(_mutate)
    return session


def resume() -> store.Session:
    active = _read_active()
    if not active:
        raise RecorderError("no active recording")
    if not _alive(active["pid"]):
        _clear_active()
        raise RecorderError("recorder process is gone")

    session = store.Session(session_id=Path(active["session_dir"]).name,
                            dir=Path(active["session_dir"]))
    log_path = session.dir / "recorder.log"
    pos = _log_pos(log_path)
    _send_command(Path(active["ctrl"]), "RESUME")
    evt, _ = _tail_log_for_event(log_path, {"resumed", "error"}, timeout=15.0, start_pos=pos)
    if evt.get("event") == "error":
        raise RecorderError(f"resume failed: {evt.get('message')}")

    def _mutate(meta: dict) -> None:
        pauses = meta.get("pauses") or []
        if pauses and "resumed_at" not in pauses[-1]:
            pauses[-1]["resumed_at"] = time.time()
            pauses[-1]["duration"] = pauses[-1]["resumed_at"] - pauses[-1]["paused_at"]
    session.update_meta(_mutate)
    return session


def mic_off() -> store.Session:
    active = _read_active()
    if not active:
        raise RecorderError("no active recording")
    if not _alive(active["pid"]):
        _clear_active()
        raise RecorderError("recorder process is gone")

    session = store.Session(session_id=Path(active["session_dir"]).name,
                            dir=Path(active["session_dir"]))
    log_path = session.dir / "recorder.log"
    pos = _log_pos(log_path)
    _send_command(Path(active["ctrl"]), "MIC_OFF")
    evt, _ = _tail_log_for_event(log_path, {"mic_off", "error"}, timeout=15.0, start_pos=pos)
    if evt.get("event") == "error":
        raise RecorderError(f"mic off failed: {evt.get('message')}")

    def _mutate(meta: dict) -> None:
        meta.setdefault("mic_intervals", []).append({"off_at": time.time()})
    session.update_meta(_mutate)
    notify("win-rec", "MIC OFF")
    return session


def mic_on() -> store.Session:
    active = _read_active()
    if not active:
        raise RecorderError("no active recording")
    if not _alive(active["pid"]):
        _clear_active()
        raise RecorderError("recorder process is gone")

    session = store.Session(session_id=Path(active["session_dir"]).name,
                            dir=Path(active["session_dir"]))
    log_path = session.dir / "recorder.log"
    pos = _log_pos(log_path)
    _send_command(Path(active["ctrl"]), "MIC_ON")
    evt, _ = _tail_log_for_event(log_path, {"mic_on", "error"}, timeout=15.0, start_pos=pos)
    if evt.get("event") == "error":
        raise RecorderError(f"mic on failed: {evt.get('message')}")

    def _mutate(meta: dict) -> None:
        intervals = meta.get("mic_intervals") or []
        if intervals and "on_at" not in intervals[-1]:
            intervals[-1]["on_at"] = time.time()
            intervals[-1]["duration"] = intervals[-1]["on_at"] - intervals[-1]["off_at"]
    session.update_meta(_mutate)
    notify("win-rec", "MIC ON")
    return session


def stop() -> store.Session:
    active = _read_active()
    if not active:
        raise RecorderError("no active recording")
    pid = active["pid"]
    ctrl_path = Path(active["ctrl"])
    session_dir = Path(active["session_dir"])
    session = store.Session(session_id=session_dir.name, dir=session_dir)

    if not _alive(pid):
        _clear_active()
        raise RecorderError("recorder process is gone; recording may be incomplete")

    log_path = session.dir / "recorder.log"
    pos = _log_pos(log_path)
    _send_command(ctrl_path, "STOP")
    evt, _ = _tail_log_for_event(log_path, {"stopped", "error"}, timeout=30.0, start_pos=pos)

    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    if _alive(pid):
        try:
            import signal as _signal
            os.kill(pid, _signal.SIGTERM)
        except (ProcessLookupError, AttributeError, OSError):
            pass

    if ctrl_path.exists():
        ctrl_path.unlink()

    if evt.get("event") == "error":
        raise RecorderError(f"recorder stop failed: {evt.get('message', 'unknown error')}")

    mic_segs = evt.get("mic_segments") or []

    meta = session.read_meta()
    mic_msg = _concat_mic_with_silence(session, mic_segs, evt.get("duration", 0.0))
    print(f"[stop] {mic_msg}", file=sys.stderr)

    mic_duration = _measure_m4a_seconds(session.mic_audio) or 0.0
    meta["stopped_at"] = time.time()
    meta["mic_segments"] = mic_segs
    meta["duration"] = evt.get("duration") or mic_duration
    meta["pause_count"] = len(meta.get("pauses", []))
    session.write_meta(meta)

    _clear_active()
    store.clear_current()

    _sanity_check_audio(session, meta["duration"])
    return session


def _sanity_check_audio(session: store.Session, duration_sec: float) -> None:
    problems = []
    if not session.mic_audio.exists():
        problems.append(f"mic: file missing at {session.mic_audio}")
    elif session.mic_audio.stat().st_size == 0:
        problems.append(f"mic: 0 bytes — mic.m4a")
    if problems:
        details = "\n  ".join(problems)
        raise RecorderError(
            f"recording completed but audio files are invalid:\n  {details}\n"
            f"See {session.dir / 'recorder.log'} for writer state."
        )
