"""Tests for win_rec/tray.py — state machine, pid check, exe resolution."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Stub out pystray and PIL before importing tray so the test suite runs
# without those optional GUI libraries installed in the test environment.
pystray_stub = MagicMock()
pil_stub = MagicMock()
sys.modules.setdefault("pystray", pystray_stub)
sys.modules.setdefault("PIL", pil_stub)
sys.modules.setdefault("PIL.Image", pil_stub)
sys.modules.setdefault("PIL.ImageDraw", pil_stub)

from win_rec import tray  # noqa: E402  (must come after stub setup)


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_log(path: Path, events: list[str]) -> None:
    path.write_bytes(b"\n".join(json.dumps({"event": e}).encode() for e in events))


def _active_json(tmp_path: Path, pid: int, session_dir: Path | None = None) -> Path:
    data: dict = {"pid": pid}
    if session_dir is not None:
        data["session_dir"] = str(session_dir)
    af = tmp_path / "active.json"
    af.write_text(json.dumps(data))
    return af


# ── _alive ────────────────────────────────────────────────────────────────────

class TestAlive:
    def test_current_process_is_alive(self):
        assert tray._alive(os.getpid()) is True

    def test_dead_pid_returns_false(self):
        # PID 0 is the kernel on Linux/macOS and raises PermissionError (OSError)
        # or PermissionError; either way _alive should return False.
        with patch("os.kill", side_effect=OSError):
            assert tray._alive(99999) is False

    def test_process_lookup_error_returns_false(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            assert tray._alive(99999) is False


# ── _read_active ──────────────────────────────────────────────────────────────

class TestReadActive:
    def test_missing_file_returns_none(self, tmp_path):
        with patch("win_rec.tray._ACTIVE_FILE", tmp_path / "active.json"):
            assert tray._read_active() is None

    def test_valid_json_returned(self, tmp_path):
        af = tmp_path / "active.json"
        af.write_text(json.dumps({"pid": 123, "session_dir": "/tmp/s"}))
        with patch("win_rec.tray._ACTIVE_FILE", af):
            result = tray._read_active()
        assert result == {"pid": 123, "session_dir": "/tmp/s"}

    def test_truncated_json_returns_none(self, tmp_path):
        af = tmp_path / "active.json"
        af.write_text('{"pid": 12')
        with patch("win_rec.tray._ACTIVE_FILE", af):
            assert tray._read_active() is None

    def test_empty_file_returns_none(self, tmp_path):
        af = tmp_path / "active.json"
        af.write_bytes(b"")
        with patch("win_rec.tray._ACTIVE_FILE", af):
            assert tray._read_active() is None


# ── _get_state ────────────────────────────────────────────────────────────────

class TestGetState:
    def _patch(self, tmp_path, pid, session_dir=None, alive=True):
        af = _active_json(tmp_path, pid, session_dir)
        return (
            patch("win_rec.tray._ACTIVE_FILE", af),
            patch.object(tray, "_alive", return_value=alive),
        )

    def test_no_active_file_is_idle(self, tmp_path):
        with patch("win_rec.tray._ACTIVE_FILE", tmp_path / "active.json"):
            assert tray._get_state() == "idle"

    def test_dead_pid_is_idle(self, tmp_path):
        p1, p2 = self._patch(tmp_path, pid=9999, session_dir=tmp_path, alive=False)
        with p1, p2:
            assert tray._get_state() == "idle"

    def test_alive_no_log_is_recording(self, tmp_path):
        # session_dir exists but no recorder.log → default to "recording"
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "recording"

    def test_missing_session_dir_key_is_recording(self, tmp_path):
        # active.json has no session_dir key at all
        af = tmp_path / "active.json"
        af.write_text(json.dumps({"pid": 1}))
        with patch("win_rec.tray._ACTIVE_FILE", af), \
             patch.object(tray, "_alive", return_value=True):
            assert tray._get_state() == "recording"

    def test_last_event_started_is_recording(self, tmp_path):
        _write_log(tmp_path / "recorder.log", ["started"])
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "recording"

    def test_last_event_paused_is_paused(self, tmp_path):
        _write_log(tmp_path / "recorder.log", ["started", "paused"])
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "paused"

    def test_resumed_after_paused_is_recording(self, tmp_path):
        _write_log(tmp_path / "recorder.log", ["started", "paused", "resumed"])
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "recording"

    def test_multiple_pause_resume_cycles(self, tmp_path):
        _write_log(tmp_path / "recorder.log",
                   ["started", "paused", "resumed", "paused", "resumed", "paused"])
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "paused"

    def test_corrupt_log_lines_skipped(self, tmp_path):
        log = tmp_path / "recorder.log"
        log.write_bytes(
            b'{"event": "started"}\n'
            b'NOT JSON AT ALL\n'
            b'{"event": "paused"}\n'
            b'\xff\xfe invalid utf-8\n'
        )
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "paused"

    def test_unknown_events_do_not_change_state(self, tmp_path):
        _write_log(tmp_path / "recorder.log", ["started", "some_unknown_event"])
        p1, p2 = self._patch(tmp_path, pid=1, session_dir=tmp_path, alive=True)
        with p1, p2:
            assert tray._get_state() == "recording"


# ── _win_rec_exe ──────────────────────────────────────────────────────────────

class TestWinRecExe:
    def test_dev_mode_finds_via_which(self, tmp_path):
        fake_exe = tmp_path / "win-rec"
        fake_exe.touch()
        with patch.object(sys, "frozen", False, create=True), \
             patch("shutil.which", return_value=str(fake_exe)):
            assert tray._win_rec_exe() == str(fake_exe)

    def test_dev_mode_raises_when_not_in_path(self):
        with patch.object(sys, "frozen", False, create=True), \
             patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found in PATH"):
                tray._win_rec_exe()

    def test_frozen_finds_beside_executable(self, tmp_path):
        fake_exe = tmp_path / "win-rec.exe"
        fake_exe.touch()
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(tmp_path / "tray.exe")):
            assert tray._win_rec_exe() == str(fake_exe)

    def test_frozen_raises_when_not_beside_executable(self, tmp_path):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(tmp_path / "tray.exe")):
            with pytest.raises(RuntimeError, match="same folder"):
                tray._win_rec_exe()

    def test_frozen_does_not_fall_through_to_path(self, tmp_path, monkeypatch):
        which_called = []
        monkeypatch.setattr("shutil.which", lambda _: which_called.append(True) or "/usr/bin/win-rec")
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(tmp_path / "tray.exe")):
            with pytest.raises(RuntimeError):
                tray._win_rec_exe()
        assert not which_called, "shutil.which must not be called in frozen mode"
