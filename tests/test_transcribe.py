"""Tests for transcribe.py — Segment, _fmt_timestamp, _dedupe_overlap, _result_to_segments."""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from win_rec.transcribe import Segment, _fmt_timestamp, _dedupe_overlap, _result_to_segments


class TestSegment:
    def test_to_dict_roundtrip(self):
        s = Segment(speaker="me", start=1.5, end=3.0, text="hello", source="mic")
        d = s.to_dict()
        assert d == {"speaker": "me", "start": 1.5, "end": 3.0, "text": "hello", "source": "mic"}

    def test_to_markdown(self):
        s = Segment(speaker="Speaker A", start=65.0, end=68.0, text="test text", source="mic")
        md = s.to_markdown()
        assert "01:05" in md
        assert "Speaker A" in md
        assert "test text" in md

    def test_to_markdown_with_hours(self):
        s = Segment(speaker="X", start=3661.0, end=3662.0, text="long", source="mic")
        assert "01:01:01" in s.to_markdown()


class TestFmtTimestamp:
    def test_seconds_only(self):
        assert _fmt_timestamp(5.0) == "00:05"

    def test_minutes(self):
        assert _fmt_timestamp(90.0) == "01:30"

    def test_hours(self):
        assert _fmt_timestamp(3661.0) == "01:01:01"

    def test_zero(self):
        assert _fmt_timestamp(0.0) == "00:00"


class TestDedupeOverlap:
    def _seg(self, start, end, text):
        return Segment(speaker="X", start=start, end=end, text=text, source="mic")

    def test_no_dupes(self):
        segs = [self._seg(0, 1, "a"), self._seg(2, 3, "b")]
        assert _dedupe_overlap(segs) == segs

    def test_exact_dupe_at_boundary(self):
        # condition: seg.start < seen[-1].end - 0.1 → 1.8 < 2.0 - 0.1 = 1.9 ✓
        segs = [
            self._seg(0.0, 2.0, "same text"),
            self._seg(1.8, 3.5, "same text"),
        ]
        result = _dedupe_overlap(segs)
        assert len(result) == 1

    def test_same_text_no_overlap(self):
        # same text but no time overlap → both kept
        segs = [self._seg(0, 1, "same"), self._seg(5, 6, "same")]
        assert len(_dedupe_overlap(segs)) == 2

    def test_empty(self):
        assert _dedupe_overlap([]) == []


class TestResultToSegments:
    def _make_result(self, segments=None, text="", duration=0.0):
        return {"segments": segments or [], "text": text, "duration": duration}

    def test_segments_path(self):
        raw = self._make_result(segments=[
            {"text": "hello", "start": 0.5, "end": 1.5, "no_speech_prob": 0.0},
            {"text": "world", "start": 2.0, "end": 3.0, "no_speech_prob": 0.0},
        ])
        segs = _result_to_segments(raw, 0.0, "me", "mic")
        assert len(segs) == 2
        assert segs[0].text == "hello"
        assert segs[1].text == "world"

    def test_offset_applied(self):
        raw = self._make_result(segments=[
            {"text": "hi", "start": 1.0, "end": 2.0, "no_speech_prob": 0.0},
        ])
        segs = _result_to_segments(raw, 10.0, "me", "mic")
        assert segs[0].start == 11.0
        assert segs[0].end == 12.0

    def test_high_no_speech_prob_filtered(self):
        raw = self._make_result(segments=[
            {"text": "noise", "start": 0.0, "end": 1.0, "no_speech_prob": 0.9},
        ])
        segs = _result_to_segments(raw, 0.0, "me", "mic")
        assert segs == []

    def test_fallback_to_text(self):
        raw = {"segments": [], "text": "fallback text", "duration": 5.0}
        segs = _result_to_segments(raw, 0.0, "me", "mic")
        assert len(segs) == 1
        assert segs[0].text == "fallback text"
        assert segs[0].end == 5.0

    def test_empty_text_segment_skipped(self):
        raw = self._make_result(segments=[
            {"text": "  ", "start": 0.0, "end": 1.0, "no_speech_prob": 0.0},
        ])
        segs = _result_to_segments(raw, 0.0, "me", "mic")
        assert segs == []
