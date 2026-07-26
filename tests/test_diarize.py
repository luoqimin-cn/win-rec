"""Tests for diarize.py — merge_dual_track and dedup logic."""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from win_rec.transcribe import Segment
from win_rec.diarize import merge_dual_track, _text_similar, _drop_cross_track_dupes


def _seg(speaker, start, end, text, source="mic"):
    return Segment(speaker=speaker, start=start, end=end, text=text, source=source)


class TestTextSimilar:
    def test_identical_strings(self):
        assert _text_similar("hello world", "hello world", 0.6)

    def test_empty_strings(self):
        assert not _text_similar("", "hello", 0.6)
        assert not _text_similar("hello", "", 0.6)
        assert not _text_similar("", "", 0.6)

    def test_completely_different(self):
        assert not _text_similar("apple pie", "quantum mechanics", 0.6)

    def test_similar_with_punctuation(self):
        # punctuation stripped, alphanumeric only compared
        assert _text_similar("Hello, world!", "Hello world", 0.6)

    def test_length_ratio_filter(self):
        # very different lengths → False before SequenceMatcher
        assert not _text_similar("a", "abcdefghijklmnopqrstuvwxyz", 0.6)

    def test_threshold_boundary(self):
        assert _text_similar("abcde", "abcde", 1.0)
        assert not _text_similar("abcde", "fghij", 1.0)


class TestDropCrossTrackDupes:
    def test_empty_inputs(self):
        assert _drop_cross_track_dupes([], []) == []
        assert _drop_cross_track_dupes([_seg("me", 0, 1, "hi")], []) == [_seg("me", 0, 1, "hi")]
        assert _drop_cross_track_dupes([], [_seg("other", 0, 1, "hi")]) == []

    def test_no_overlap_kept(self):
        mic = [_seg("me", 0, 1, "unique mic text")]
        sys = [_seg("other", 5, 6, "completely different")]
        result = _drop_cross_track_dupes(mic, sys)
        assert result == mic

    def test_duplicate_within_window_dropped(self):
        mic = [_seg("me", 0.5, 1.5, "hello there")]
        sys = [_seg("other", 0.0, 1.0, "hello there")]
        result = _drop_cross_track_dupes(mic, sys)
        assert result == []

    def test_similar_text_within_window_dropped(self):
        mic = [_seg("me", 0.0, 1.0, "meeting starts now")]
        sys = [_seg("other", 0.2, 1.2, "meeting starts now")]
        result = _drop_cross_track_dupes(mic, sys)
        assert result == []

    def test_dissimilar_text_kept(self):
        mic = [_seg("me", 0.0, 1.0, "I think we should proceed")]
        sys = [_seg("other", 0.2, 1.2, "can you hear me clearly")]
        result = _drop_cross_track_dupes(mic, sys)
        assert result == mic


class TestMergeDualTrack:
    def test_empty_inputs(self):
        assert merge_dual_track([], []) == []

    def test_merge_sorted_by_start(self):
        mic = [_seg("me", 2.0, 3.0, "second")]
        sys = [_seg("other", 0.0, 1.0, "first")]
        result = merge_dual_track(mic, sys)
        assert result[0].text == "first"
        assert result[1].text == "second"

    def test_dedup_applied_during_merge(self):
        # mic bleed-through: same text, overlapping time
        mic = [_seg("me", 0.0, 1.0, "shared audio")]
        sys = [_seg("other", 0.1, 1.1, "shared audio")]
        result = merge_dual_track(mic, sys)
        # mic duplicate should be dropped; only system track kept
        assert len(result) == 1
        assert result[0].speaker == "other"

    def test_mic_only_passthrough(self):
        mic = [_seg("me", 0.0, 1.0, "only mic"), _seg("me", 2.0, 3.0, "more mic")]
        result = merge_dual_track(mic, [])
        assert len(result) == 2

    def test_system_only_passthrough(self):
        sys = [_seg("other", 0.0, 1.0, "only system")]
        result = merge_dual_track([], sys)
        assert len(result) == 1
