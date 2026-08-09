"""Tests de `worker/app/pipeline/subtitles.py`. Correr desde `worker/`
(ver README): `pytest ../tests/test_subtitles.py`."""
import pytest

from app.pipeline.subtitles import build_srt_content, fmt_ts, make_srt, sanitize_filename
from app.pipeline.translate import TranslatedSegment


class TestFmtTs:
    def test_zero(self):
        assert fmt_ts(0) == "00:00:00,000"

    def test_seconds_and_millis(self):
        assert fmt_ts(65.5) == "00:01:05,500"

    def test_hours(self):
        assert fmt_ts(3661.25) == "01:01:01,250"

    def test_rounds_millis(self):
        # 1.9999s -> no debería truncar a 999ms perdiendo el segundo
        assert fmt_ts(1.9999) in ("00:00:01,999", "00:00:02,000")


class TestBuildSrtContent:
    def test_empty(self):
        assert build_srt_content([]) == ""

    def test_single_segment_format(self):
        segs = [TranslatedSegment(start=0.0, end=2.5, text_original="Hello", text="Hola")]
        content = build_srt_content(segs)
        lines = content.split("\n")
        assert lines[0] == "1"
        assert lines[1] == "00:00:00,000 --> 00:00:02,500"
        assert lines[2] == "Hola"
        assert lines[3] == ""

    def test_multiple_segments_numbered_sequentially(self):
        segs = [
            TranslatedSegment(start=0.0, end=1.0, text_original="a", text="a"),
            TranslatedSegment(start=1.0, end=2.0, text_original="b", text="b"),
            TranslatedSegment(start=2.0, end=3.0, text_original="c", text="c"),
        ]
        content = build_srt_content(segs)
        indices = [line for line in content.split("\n") if line.strip().isdigit()]
        assert indices == ["1", "2", "3"]

    def test_uses_translated_text_not_original(self):
        segs = [TranslatedSegment(start=0.0, end=1.0, text_original="Hello world", text="Hola mundo")]
        content = build_srt_content(segs)
        assert "Hola mundo" in content
        assert "Hello world" not in content


def test_make_srt_writes_file(tmp_path):
    segs = [TranslatedSegment(start=0.0, end=1.0, text_original="hi", text="hola")]
    path = make_srt(segs, tmp_path / "out.srt")
    assert path.exists()
    assert "hola" in path.read_text(encoding="utf-8")


class TestSanitizeFilename:
    def test_normal_title(self):
        assert sanitize_filename("My Cool Video") == "My Cool Video"

    def test_strips_unsafe_chars(self):
        result = sanitize_filename("Video: The <Best> One! (2024)")
        assert ":" not in result
        assert "<" not in result and ">" not in result
        assert "!" not in result
        assert "(" not in result and ")" not in result

    def test_blocks_path_traversal(self):
        result = sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_blocks_path_traversal_backslash(self):
        result = sanitize_filename("..\\..\\windows\\system32")
        assert "\\" not in result
        assert ".." not in result

    def test_empty_title_falls_back(self):
        assert sanitize_filename("") == "video"

    def test_only_unsafe_chars_falls_back(self):
        assert sanitize_filename("!!!///:::") == "video"

    def test_only_dots_falls_back(self):
        assert sanitize_filename("...") == "video"

    def test_truncates_to_max_length(self):
        long_title = "A" * 200
        result = sanitize_filename(long_title, max_length=80)
        assert len(result) <= 80

    def test_reserved_windows_name_gets_suffixed(self):
        result = sanitize_filename("CON")
        assert result.lower() != "con"

    def test_collapses_whitespace(self):
        assert sanitize_filename("Too    Many     Spaces") == "Too Many Spaces"

    def test_custom_fallback(self):
        assert sanitize_filename("", fallback="job123") == "job123"
