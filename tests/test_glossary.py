"""Tests for glossary.py — load, as_prompt_fragment, scan result parsing."""
from __future__ import annotations

import sys, os, textwrap
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pathlib import Path
from unittest.mock import patch


class TestGlossaryLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import load
            assert load() == {}

    def test_basic_flat_load(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text(textwrap.dedent("""\
            people:
              Alice: [ali, 爱丽丝]
            companies:
              Anthropic: []
        """), encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import load
            g = load()
        assert "Alice" in g
        assert g["Alice"] == ["ali", "爱丽丝"]
        assert "Anthropic" in g
        assert g["Anthropic"] == []

    def test_yaml_error_returns_empty_with_warning(self, tmp_path, capsys):
        path = tmp_path / "glossary.yaml"
        path.write_text(":\tinvalid yaml\t:\n  - bad", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import load
            result = load(strict=False)
        assert result == {}
        captured = capsys.readouterr()
        assert "GLOSSARY YAML ERROR" in captured.err

    def test_yaml_error_strict_raises(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text(":\tinvalid\n", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import load, GlossaryError
            with pytest.raises(GlossaryError):
                load(strict=True)

    def test_non_dict_top_level_returns_empty(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text("- item1\n- item2\n", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import load
            assert load() == {}

    def test_numeric_variant_converted_to_str(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text("terms:\n  K8S:\n    - 8\n", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import load
            g = load()
        assert g["K8S"] == ["8"]


class TestAsPromptFragment:
    def test_empty_glossary_returns_empty_string(self, tmp_path):
        path = tmp_path / "missing.yaml"
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import as_prompt_fragment
            assert as_prompt_fragment() == ""

    def test_populated_glossary_includes_canonical(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text("terms:\n  Kubernetes:\n    - k8s\n", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import as_prompt_fragment
            frag = as_prompt_fragment()
        assert "Kubernetes" in frag
        assert "k8s" in frag

    def test_no_variants_rendered_without_variants_line(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text("terms:\n  Anthropic: []\n", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import as_prompt_fragment
            frag = as_prompt_fragment()
        assert "Anthropic" in frag


class TestEnsureSeedFile:
    def test_creates_file_if_missing(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import ensure_seed_file
            ensure_seed_file()
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "_suspected_asr_errors" in content

    def test_does_not_overwrite_existing(self, tmp_path):
        path = tmp_path / "glossary.yaml"
        path.write_text("# my custom content\n", encoding="utf-8")
        with patch("win_rec.glossary.GLOSSARY_PATH", path):
            from win_rec.glossary import ensure_seed_file
            ensure_seed_file()
        assert path.read_text(encoding="utf-8") == "# my custom content\n"
