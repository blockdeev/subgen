"""Tests de `worker/app/pipeline/segments.py`. Correr desde `worker/`:
`pytest ../tests/test_segments.py`."""
import pytest

from app.pipeline.segments import boundaries_from_durations, split_cues_by_segment
from app.pipeline.translate import TranslatedSegment


class TestBoundariesFromDurations:
    def test_single_segment(self):
        assert boundaries_from_durations([120.0]) == [0.0, 120.0]

    def test_multiple_segments_accumulate(self):
        assert boundaries_from_durations([100.0, 95.5, 102.3]) == pytest.approx(
            [0.0, 100.0, 195.5, 297.8]
        )

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            boundaries_from_durations([])

    def test_zero_duration_raises(self):
        with pytest.raises(ValueError):
            boundaries_from_durations([100.0, 0.0, 50.0])

    def test_negative_duration_raises(self):
        with pytest.raises(ValueError):
            boundaries_from_durations([-5.0])


class TestSplitCuesBySegment:
    def _cue(self, start: float, end: float, text: str = "x") -> TranslatedSegment:
        return TranslatedSegment(start=start, end=end, text_original=text, text=text)

    def test_cue_entirely_within_one_segment(self):
        cues = [self._cue(5.0, 8.0)]
        boundaries = [0.0, 10.0, 20.0]
        result = split_cues_by_segment(cues, boundaries)
        assert len(result[0]) == 1
        assert result[0][0].start == pytest.approx(5.0)
        assert result[0][0].end == pytest.approx(8.0)
        assert len(result[1]) == 0

    def test_cue_crossing_boundary_appears_truncated_in_both(self):
        """El caso explícito pedido: cue de 9:58 (598s) a 10:02 (602s),
        corte en 10:00 (600s) -- tiene que aparecer recortado en los dos
        segmentos, nunca completo en uno solo ni descartado."""
        cues = [self._cue(598.0, 602.0, text="hola")]
        boundaries = [0.0, 600.0, 1200.0]

        result = split_cues_by_segment(cues, boundaries)

        assert len(result[0]) == 1, "el cue no llegó al segmento 0"
        assert len(result[1]) == 1, "el cue no llegó al segmento 1"

        # Segmento 0: el cue queda desde 598s (local) hasta el final (600s local)
        seg0_cue = result[0][0]
        assert seg0_cue.start == pytest.approx(598.0)
        assert seg0_cue.end == pytest.approx(600.0)
        assert seg0_cue.text == "hola"

        # Segmento 1: el cue arranca en 0 (local, justo al principio) y
        # dura hasta 602 - 600 = 2s
        seg1_cue = result[1][0]
        assert seg1_cue.start == pytest.approx(0.0)
        assert seg1_cue.end == pytest.approx(2.0)
        assert seg1_cue.text == "hola"

    def test_cue_spanning_three_segments(self):
        cues = [self._cue(5.0, 25.0)]
        boundaries = [0.0, 10.0, 20.0, 30.0]  # 3 segmentos de 10s

        result = split_cues_by_segment(cues, boundaries)

        assert len(result[0]) == 1 and result[0][0].start == pytest.approx(5.0) \
            and result[0][0].end == pytest.approx(10.0)
        assert len(result[1]) == 1 and result[1][0].start == pytest.approx(0.0) \
            and result[1][0].end == pytest.approx(10.0)
        assert len(result[2]) == 1 and result[2][0].start == pytest.approx(0.0) \
            and result[2][0].end == pytest.approx(5.0)

    def test_cue_ending_exactly_at_boundary_not_duplicated_in_next_segment(self):
        cues = [self._cue(5.0, 10.0)]  # termina justo en la frontera
        boundaries = [0.0, 10.0, 20.0]

        result = split_cues_by_segment(cues, boundaries)

        assert len(result[0]) == 1
        assert len(result[1]) == 0, "no debe aparecer un cue de duración cero en el segmento siguiente"

    def test_cue_starting_exactly_at_boundary_not_duplicated_in_previous_segment(self):
        cues = [self._cue(10.0, 15.0)]  # arranca justo en la frontera
        boundaries = [0.0, 10.0, 20.0]

        result = split_cues_by_segment(cues, boundaries)

        assert len(result[0]) == 0, "no debe aparecer un cue de duración cero en el segmento anterior"
        assert len(result[1]) == 1
        assert result[1][0].start == pytest.approx(0.0)

    def test_multiple_cues_distributed_correctly(self):
        cues = [
            self._cue(1.0, 3.0, "a"),
            self._cue(4.0, 6.0, "b"),
            self._cue(9.0, 12.0, "c"),  # cruza la frontera de los 10s
            self._cue(15.0, 18.0, "d"),
        ]
        boundaries = [0.0, 10.0, 20.0]

        result = split_cues_by_segment(cues, boundaries)

        assert [c.text for c in result[0]] == ["a", "b", "c"]
        assert [c.text for c in result[1]] == ["c", "d"]

    def test_preserves_original_and_translated_text(self):
        cue = TranslatedSegment(start=1.0, end=3.0, text_original="hello", text="hola")
        result = split_cues_by_segment([cue], [0.0, 10.0])
        assert result[0][0].text_original == "hello"
        assert result[0][0].text == "hola"

    def test_empty_cues_list_returns_empty_segments(self):
        result = split_cues_by_segment([], [0.0, 10.0, 20.0])
        assert result == [[], []]

    def test_too_few_boundaries_raises(self):
        with pytest.raises(ValueError):
            split_cues_by_segment([self._cue(1.0, 2.0)], [0.0])
