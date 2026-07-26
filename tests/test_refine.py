"""Tests for refine.py — _parse_output, _format_input."""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from win_rec.transcribe import Segment
from win_rec.refine import _parse_output, _format_input


def _seg(i, text="test"):
    return Segment(speaker="me", start=float(i), end=float(i + 1), text=text, source="mic")


class TestParseOutput:
    def test_clean_json_array(self):
        raw = '[{"text": "hello"}, {"text": "world"}]'
        result = _parse_output(raw, expected=2)
        assert result == ["hello", "world"]

    def test_with_code_fence(self):
        raw = '```json\n[{"text": "a"}, {"text": "b"}]\n```'
        result = _parse_output(raw, expected=2)
        assert result == ["a", "b"]

    def test_prose_before_json(self):
        raw = 'Here is the output:\n[{"text": "corrected"}]'
        result = _parse_output(raw, expected=1)
        assert result == ["corrected"]

    def test_empty_text_hallucination(self):
        raw = '[{"text": "real"}, {"text": ""}]'
        result = _parse_output(raw, expected=2)
        assert result == ["real", ""]

    def test_null_text_normalized_to_empty(self):
        raw = '[{"text": null}]'
        result = _parse_output(raw, expected=1)
        assert result == [""]

    def test_count_mismatch_too_many_truncated(self):
        raw = '[{"text": "a"}, {"text": "b"}, {"text": "c"}]'
        result = _parse_output(raw, expected=2)
        assert result == ["a", "b"]

    def test_count_mismatch_too_few_padded(self):
        raw = '[{"text": "a"}]'
        result = _parse_output(raw, expected=3)
        assert result == ["a", "", ""]

    def test_invalid_json_returns_none(self):
        assert _parse_output("not json at all", expected=1) is None

    def test_missing_text_field_returns_none(self):
        raw = '[{"content": "wrong field"}]'
        assert _parse_output(raw, expected=1) is None

    def test_not_array_returns_none(self):
        assert _parse_output('{"text": "not array"}', expected=1) is None

    def test_no_bracket_returns_none(self):
        assert _parse_output("no bracket here", expected=1) is None

    def test_code_fence_json_capitalized(self):
        raw = '```JSON\n[{"text": "caps fence"}]\n```'
        result = _parse_output(raw, expected=1)
        assert result == ["caps fence"]

    def test_prose_bracket_not_confused_with_fence_bracket(self):
        # prose has [13-18] which contains a bracket — should not confuse parser
        raw = 'I noticed items [13-18] need corrections.\n```json\n[{"text": "ok"}]\n```'
        result = _parse_output(raw, expected=1)
        assert result == ["ok"]


class TestFormatInput:
    def test_basic_format(self):
        segs = [_seg(0, "hello world"), _seg(1, "goodbye")]
        out = _format_input(segs)
        lines = out.splitlines()
        assert lines[0] == "0. [me] hello world"
        assert lines[1] == "1. [me] goodbye"

    def test_newlines_in_text_replaced(self):
        segs = [_seg(0, "line one\nline two")]
        out = _format_input(segs)
        assert "\n" not in out.splitlines()[0].split("] ", 1)[1]

    def test_empty_segments(self):
        assert _format_input([]) == ""
