"""Tests for summarize.py — _fmt_ts, _format_transcript, _build_user_content, retry logic."""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from win_rec.transcribe import Segment
from win_rec.summarize import _fmt_ts, _format_transcript, _build_user_content, _is_retryable


def _seg(speaker, start, text):
    return Segment(speaker=speaker, start=start, end=start + 1, text=text, source="mic")


class TestFmtTs:
    def test_zero(self):
        assert _fmt_ts(0.0) == "00:00"

    def test_minutes(self):
        assert _fmt_ts(90.5) == "01:30"

    def test_hours(self):
        assert _fmt_ts(3661.0) == "61:01"  # no hour component in this formatter


class TestFormatTranscript:
    def test_single_segment(self):
        segs = [_seg("Alice", 60.0, "Hello")]
        out = _format_transcript(segs)
        assert "[01:00] Alice: Hello" in out

    def test_multiple_segments(self):
        segs = [_seg("A", 0.0, "first"), _seg("B", 30.0, "second")]
        out = _format_transcript(segs)
        lines = out.splitlines()
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]


class TestBuildUserContent:
    def test_no_meta(self):
        segs = [_seg("me", 0.0, "text")]
        content = _build_user_content(segs, None)
        assert "会议转写" in content
        assert "me" in content

    def test_with_meta_name(self):
        segs = [_seg("me", 0.0, "text")]
        meta = {"name": "产品评审", "started_at_iso": "2024-01-15T10:30:00", "duration": 3600}
        content = _build_user_content(segs, meta)
        assert "产品评审" in content
        assert "2024-01-15T10:30:00" in content
        assert "3600秒" in content

    def test_partial_meta(self):
        segs = [_seg("me", 0.0, "text")]
        meta = {"name": "会议"}  # no started_at_iso or duration
        content = _build_user_content(segs, meta)
        assert "会议" in content

    def test_empty_segments(self):
        content = _build_user_content([], None)
        assert "会议转写" in content


class TestIsRetryable:
    def test_auth_error_not_retryable(self):
        class AuthenticationError(Exception):
            pass
        assert not _is_retryable(AuthenticationError("bad key"))

    def test_bad_request_not_retryable(self):
        class BadRequestError(Exception):
            pass
        assert not _is_retryable(BadRequestError("invalid"))

    def test_generic_error_is_retryable(self):
        assert _is_retryable(ConnectionError("timeout"))
        assert _is_retryable(RuntimeError("server 500"))
        assert _is_retryable(Exception("network error"))

    def test_permission_denied_not_retryable(self):
        class PermissionDeniedError(Exception):
            pass
        assert not _is_retryable(PermissionDeniedError("no access"))
