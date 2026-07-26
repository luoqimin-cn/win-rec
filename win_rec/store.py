from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config


# Session dir names look like YYYY-MM-DD_HH-MM-SS, optionally with a suffix
# (e.g. "_merged"). Any other subdirectory in recordings/ is ignored so that
# users can add helper folders (e.g. Backup) without them being scanned by
# win-rec list / run-daily.
_SESSION_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")


@dataclass
class Session:
    session_id: str
    dir: Path
    name: str | None = None

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def system_audio(self) -> Path:
        return self.dir / "system.m4a"

    @property
    def mic_audio(self) -> Path:
        return self.dir / "mic.m4a"

    @property
    def transcript_json(self) -> Path:
        return self.dir / "transcript.json"

    @property
    def transcript_md(self) -> Path:
        return self.dir / "transcript.md"

    @property
    def summary_md(self) -> Path:
        return self.dir / "summary.md"

    def write_meta(self, meta: dict) -> None:
        """Atomic write via tmp file + rename.

        Atomicity here only prevents a *partial* file from being read by
        another process. It does NOT prevent lost updates in a
        read-modify-write pattern — use `update_meta()` for that.
        """
        tmp = self.meta_path.with_suffix(self.meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        tmp.replace(self.meta_path)

    def update_meta(self, mutator) -> dict:
        """Read → mutate → write meta.json under a file lock (advisory).

        Prevents lost updates when two processes race on read-modify-write.
        `mutator(meta: dict) -> None` mutates the passed-in dict in place;
        this method handles the read, the write, and the lock. Returns the
        final (post-mutation) dict for the caller's convenience.
        """
        import filelock
        lock_path = self.dir / "meta.json.lock"
        lock = filelock.FileLock(str(lock_path))
        with lock:
            current = self.read_meta()
            mutator(current)
            self.write_meta(current)
            return current

    def read_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        return json.loads(self.meta_path.read_text())


def new_session(name: str | None = None) -> Session:
    config.ensure_dirs()
    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = config.RECORDINGS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return Session(session_id=session_id, dir=session_dir, name=name)


def set_current(session: Session) -> None:
    config.ensure_dirs()
    (config.DATA_ROOT / "current.json").write_text(
        json.dumps({"session_dir": str(session.dir)})
    )


def clear_current() -> None:
    p = config.DATA_ROOT / "current.json"
    if p.exists():
        p.unlink()


def current_session() -> Session | None:
    p = config.DATA_ROOT / "current.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        target = Path(data["session_dir"])
    except Exception:
        return None
    if not target.exists():
        return None
    return Session(session_id=target.name, dir=target)


def resolve_session(ref: str) -> Session:
    if ref == "latest":
        sessions = list_sessions()
        if not sessions:
            raise FileNotFoundError("no recordings yet")
        return sessions[0]
    candidate = config.RECORDINGS_DIR / ref
    if not candidate.resolve().is_relative_to(config.RECORDINGS_DIR.resolve()):
        raise FileNotFoundError(f"session not found: {ref}")
    if not candidate.is_dir():
        raise FileNotFoundError(f"session not found: {ref}")
    return Session(session_id=candidate.name, dir=candidate)


def list_sessions() -> list[Session]:
    config.ensure_dirs()
    items = sorted(
        (
            p for p in config.RECORDINGS_DIR.iterdir()
            if p.is_dir() and _SESSION_NAME_RE.match(p.name)
        ),
        key=lambda p: p.name,
        reverse=True,
    )
    return [Session(session_id=p.name, dir=p) for p in items]


# ─── Session-level lock for `win-rec process` concurrency guard ─────────
# Prevents two `win-rec process <same_session>` from running in parallel and
# fighting for the GPU. Uses filelock.FileLock (cross-platform, auto-released
# when the holding process dies via OS file handle cleanup).

_held_locks: dict[str, object] = {}


def process_lock_path(session: Session) -> Path:
    return session.dir / ".processing.lock"


def acquire_process_lock(session: Session) -> tuple[bool, dict | None]:
    """Atomically try to claim the process lock via filelock.FileLock.

    Returns (True, None) on success — caller must release_process_lock().
    Returns (False, {pid, started_at, session_id}) if another live process
    holds it — caller should skip and inform user.
    """
    import filelock, time, os
    lock_path = process_lock_path(session)
    info_path = session.dir / ".processing.lock.json"

    lock = filelock.FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)
    except filelock.Timeout:
        try:
            data = json.loads(info_path.read_text())
        except Exception:
            data = {"pid": "?", "started_at": None, "session_id": session.session_id}
        return False, data

    # Got the lock. Write holder info for collision messages.
    try:
        info_path.write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": time.time(),
            "session_id": session.session_id,
        }, indent=2))
    except OSError:
        lock.release()
        raise

    _held_locks[str(session.dir)] = lock
    return True, None


def release_process_lock(session: Session) -> None:
    """Release the process lock. No-op if not held by us."""
    key = str(session.dir)
    lock = _held_locks.pop(key, None)
    if lock is not None:
        try:
            lock.release()
        except Exception:
            pass
    try:
        process_lock_path(session).unlink()
    except FileNotFoundError:
        pass
    try:
        (session.dir / ".processing.lock.json").unlink()
    except FileNotFoundError:
        pass


def active_recording_seconds(session: Session) -> float:
    """Compute wall time minus completed and in-progress pauses.

    Returns 0 for sessions with no started_at (never actually recorded).
    Used by `win-rec delete` to gate the protected-content threshold, and by
    status display for duration.
    """
    import time
    meta = session.read_meta()
    started = meta.get("started_at")
    if not started:
        return 0.0
    now = time.time()
    active = now - float(started)
    for p in meta.get("pauses", []):
        if "resumed_at" in p:
            active -= float(p.get("duration", p["resumed_at"] - p["paused_at"]))
        else:
            active -= now - float(p["paused_at"])
    # If session already stopped, meta may have "duration" — trust that.
    if meta.get("duration"):
        return float(meta["duration"])
    return max(0.0, active)
