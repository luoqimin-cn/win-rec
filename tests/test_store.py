"""Tests for store.py — Session, path helpers, active_recording_seconds."""
from __future__ import annotations

import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pathlib import Path
from unittest.mock import patch


class TestSessionPaths:
    def test_session_paths_derived_from_dir(self, tmp_path):
        session_dir = tmp_path / "2024-01-15_10-30-00"
        session_dir.mkdir()

        from win_rec.store import Session
        s = Session(session_id="2024-01-15_10-30-00", dir=session_dir)

        assert s.meta_path == session_dir / "meta.json"
        assert s.transcript_json == session_dir / "transcript.json"
        assert s.transcript_md == session_dir / "transcript.md"
        assert s.summary_md == session_dir / "summary.md"
        assert s.mic_audio == session_dir / "mic.m4a"
        assert s.system_audio == session_dir / "system.m4a"


class TestListSessions:
    def test_empty_recordings_dir(self, tmp_path):
        with patch("win_rec.config.RECORDINGS_DIR", tmp_path), \
             patch("win_rec.config.BACKUP_DIR", tmp_path / "Backup"):
            from win_rec import store
            sessions = store.list_sessions()
        assert sessions == []

    def test_lists_session_dirs(self, tmp_path):
        (tmp_path / "2024-01-15_10-30-00").mkdir()
        (tmp_path / "2024-01-16_11-00-00").mkdir()
        (tmp_path / "not_a_session").mkdir()  # should be excluded by pattern

        with patch("win_rec.config.RECORDINGS_DIR", tmp_path), \
             patch("win_rec.config.BACKUP_DIR", tmp_path / "Backup"):
            from win_rec import store
            sessions = store.list_sessions()

        ids = [s.session_id for s in sessions]
        assert "2024-01-15_10-30-00" in ids
        assert "2024-01-16_11-00-00" in ids
        assert "not_a_session" not in ids

    def test_sorted_newest_first(self, tmp_path):
        (tmp_path / "2024-01-14_09-00-00").mkdir()
        (tmp_path / "2024-01-15_10-30-00").mkdir()
        (tmp_path / "2024-01-16_11-00-00").mkdir()

        with patch("win_rec.config.RECORDINGS_DIR", tmp_path), \
             patch("win_rec.config.BACKUP_DIR", tmp_path / "Backup"):
            from win_rec import store
            sessions = store.list_sessions()

        assert sessions[0].session_id == "2024-01-16_11-00-00"

    def test_backup_dir_excluded(self, tmp_path):
        backup = tmp_path / "Backup"
        backup.mkdir()
        (backup / "2024-01-15_10-30-00").mkdir()

        with patch("win_rec.config.RECORDINGS_DIR", tmp_path), \
             patch("win_rec.config.BACKUP_DIR", backup):
            from win_rec import store
            sessions = store.list_sessions()
        assert sessions == []


class TestResolveSession:
    def test_resolve_by_exact_id(self, tmp_path):
        (tmp_path / "2024-01-15_10-30-00").mkdir()
        with patch("win_rec.config.RECORDINGS_DIR", tmp_path), \
             patch("win_rec.config.BACKUP_DIR", tmp_path / "Backup"):
            from win_rec import store
            s = store.resolve_session("2024-01-15_10-30-00")
        assert s is not None
        assert s.session_id == "2024-01-15_10-30-00"

    def test_resolve_missing_raises(self, tmp_path):
        with patch("win_rec.config.RECORDINGS_DIR", tmp_path), \
             patch("win_rec.config.BACKUP_DIR", tmp_path / "Backup"):
            from win_rec import store
            with pytest.raises(FileNotFoundError):
                store.resolve_session("nonexistent")

    def test_path_traversal_blocked(self, tmp_path):
        # Attempting ../.. traversal must not escape RECORDINGS_DIR
        outer = tmp_path / "outer"
        outer.mkdir()
        recordings = tmp_path / "recordings"
        recordings.mkdir()

        with patch("win_rec.config.RECORDINGS_DIR", recordings), \
             patch("win_rec.config.BACKUP_DIR", tmp_path / "Backup"):
            from win_rec import store
            with pytest.raises(FileNotFoundError):
                store.resolve_session("../../outer")


class TestActiveRecordingSeconds:
    def _make_meta(self, tmp_path, started_offset=0, pauses=None):
        """Write a meta.json and return a Session pointing at it."""
        now = time.time()
        meta = {
            "started_at": now - started_offset,
            "pauses": pauses or [],
        }
        session_dir = tmp_path / "2024-01-15_10-30-00"
        session_dir.mkdir(exist_ok=True)
        (session_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        from win_rec.store import Session
        return Session(session_id="2024-01-15_10-30-00", dir=session_dir)

    def test_no_pauses(self, tmp_path):
        from win_rec import store
        s = self._make_meta(tmp_path, started_offset=120)
        secs = store.active_recording_seconds(s)
        assert 100 <= secs <= 140  # wall time ≈ 120s

    def test_with_completed_pause(self, tmp_path):
        now = time.time()
        meta = {
            "started_at": now - 200,
            "pauses": [{"paused_at": now - 180, "resumed_at": now - 120}],  # 60s paused
        }
        session_dir = tmp_path / "2024-01-15_10-30-00"
        session_dir.mkdir(exist_ok=True)
        (session_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        from win_rec.store import Session, active_recording_seconds
        s = Session(session_id="2024-01-15_10-30-00", dir=session_dir)
        secs = active_recording_seconds(s)
        # total wall = 200s, paused = 60s, active ≈ 140s
        assert 120 <= secs <= 160

    def test_missing_meta_returns_zero(self, tmp_path):
        from win_rec.store import Session, active_recording_seconds
        session_dir = tmp_path / "2024-01-15_10-30-00"
        session_dir.mkdir()
        s = Session(session_id="2024-01-15_10-30-00", dir=session_dir)
        assert active_recording_seconds(s) == 0.0
