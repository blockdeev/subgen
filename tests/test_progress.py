"""Tests de progreso/ETA. Correr desde `worker/`:
`pytest ../tests/test_progress.py`."""
import pytest

from app.pipeline.burn import compute_progress, iter_progress_blocks
from app.tasks import STAGE_WEIGHTS_SRT, STAGE_WEIGHTS_VIDEO, _aggregate_pct


# ── iter_progress_blocks: agrupado de líneas `-progress pipe:1` ───────────

FFMPEG_SAMPLE_OUTPUT = """\
frame=100
fps=25.00
out_time_ms=4000000
speed=2.5x
progress=continue
frame=200
fps=25.00
out_time_ms=8000000
speed=2.5x
progress=continue
frame=250
fps=25.00
out_time_ms=10000000
speed=2.5x
progress=end
""".splitlines()


class TestIterProgressBlocks:
    def test_groups_into_correct_number_of_blocks(self):
        blocks = list(iter_progress_blocks(FFMPEG_SAMPLE_OUTPUT))
        assert len(blocks) == 3

    def test_each_block_has_expected_keys(self):
        blocks = list(iter_progress_blocks(FFMPEG_SAMPLE_OUTPUT))
        assert blocks[0]["out_time_ms"] == "4000000"
        assert blocks[0]["speed"] == "2.5x"
        assert blocks[2]["progress"] == "end"

    def test_ignores_blank_and_malformed_lines(self):
        lines = ["", "   ", "not_a_kv_pair", "out_time_ms=1000000", "progress=continue"]
        blocks = list(iter_progress_blocks(lines))
        assert len(blocks) == 1
        assert blocks[0]["out_time_ms"] == "1000000"

    def test_incomplete_trailing_block_is_dropped(self):
        # Si el stream corta a mitad de un bloque (proceso killeado), no
        # debe emitirse un bloque a medias.
        lines = ["out_time_ms=1000000", "speed=1.0x"]  # sin "progress=..."
        blocks = list(iter_progress_blocks(lines))
        assert blocks == []


# ── compute_progress: porcentaje + ETA a partir de un bloque ya parseado ──

class TestComputeProgress:
    def test_percentage_calculation(self):
        block = {"out_time_ms": "50000000", "speed": "1.0x"}  # 50s procesados
        event = compute_progress(block, total_seconds=100.0, elapsed_seconds=50.0)
        assert event is not None
        assert event.stage_pct == pytest.approx(50.0, abs=0.1)

    def test_eta_from_reported_speed(self):
        # 50s procesados de 100s totales, a velocidad 2x -> quedan 50s de
        # video a procesar, a 2x eso son 25s reales.
        block = {"out_time_ms": "50000000", "speed": "2.0x"}
        event = compute_progress(block, total_seconds=100.0, elapsed_seconds=25.0)
        assert event.eta_seconds == pytest.approx(25.0, abs=0.1)

    def test_eta_falls_back_to_elapsed_ratio_when_no_speed(self):
        # Sin "speed" en el bloque: ETA se estima con tiempo transcurrido.
        # 25s procesados en 25s reales de wall-clock -> rate=1.0 -> quedan
        # 75s de video a procesar -> ETA ~75s.
        block = {"out_time_ms": "25000000"}
        event = compute_progress(block, total_seconds=100.0, elapsed_seconds=25.0)
        assert event.eta_seconds == pytest.approx(75.0, abs=0.1)

    def test_returns_none_without_out_time_ms(self):
        assert compute_progress({"speed": "1.0x"}, total_seconds=100.0, elapsed_seconds=1.0) is None

    def test_returns_none_without_total_duration(self):
        block = {"out_time_ms": "1000000"}
        assert compute_progress(block, total_seconds=0.0, elapsed_seconds=1.0) is None

    def test_percentage_never_exceeds_99_9(self):
        # out_time_ms puede pasarse levemente del total por redondeo de FFmpeg.
        block = {"out_time_ms": "110000000"}  # 110s de un total de 100s
        event = compute_progress(block, total_seconds=100.0, elapsed_seconds=90.0)
        assert event.stage_pct <= 99.9

    def test_malformed_out_time_ms_returns_none(self):
        assert compute_progress({"out_time_ms": "not_a_number"}, total_seconds=100.0, elapsed_seconds=1.0) is None

    def test_stage_is_always_burning(self):
        block = {"out_time_ms": "1000000"}
        event = compute_progress(block, total_seconds=10.0, elapsed_seconds=1.0)
        assert event.stage == "burning"


# ── _aggregate_pct: progreso agregado 0-100 según pesos de cada etapa ─────

class TestAggregatePct:
    def test_video_mode_download_stage_weight(self):
        # downloading en modo video: rango (0, 20)
        assert _aggregate_pct("downloading", 0, "video") == pytest.approx(0.0)
        assert _aggregate_pct("downloading", 100, "video") == pytest.approx(20.0)
        assert _aggregate_pct("downloading", 50, "video") == pytest.approx(10.0)

    def test_video_mode_burning_stage_weight(self):
        # burning en modo video: rango (70, 100)
        assert _aggregate_pct("burning", 0, "video") == pytest.approx(70.0)
        assert _aggregate_pct("burning", 100, "video") == pytest.approx(100.0)

    def test_srt_mode_has_no_burning_stage(self):
        assert "burning" not in STAGE_WEIGHTS_SRT

    def test_srt_mode_translating_reaches_100(self):
        assert _aggregate_pct("translating", 100, "srt") == pytest.approx(100.0)

    def test_stage_weights_are_contiguous_and_span_0_to_100(self):
        for weights in (STAGE_WEIGHTS_VIDEO, STAGE_WEIGHTS_SRT):
            ranges = sorted(weights.values())
            assert ranges[0][0] == 0
            assert ranges[-1][1] == 100
            for (_, hi), (lo_next, _) in zip(ranges, ranges[1:]):
                assert hi == lo_next

    def test_clamps_out_of_range_stage_pct(self):
        assert _aggregate_pct("downloading", -10, "video") == pytest.approx(0.0)
        assert _aggregate_pct("downloading", 150, "video") == pytest.approx(20.0)


class TestMakeOnProgress:
    """Bug real de producción: sin `task_id` explícito, `update_state()`
    depende de `self.request.id`, que Celery guarda en contexto POR THREAD.
    El callback de progreso del quemado se invoca desde el thread aparte
    que lee el progreso de FFmpeg (no el thread principal de la tarea) —
    ahí `self.request.id` puede resolver a `None`, y `update_state()`
    explota adentro del propio Celery con
    "task_id must not be empty. Got None instead." en cada actualización
    de progreso durante el quemado.
    """

    def test_update_state_receives_explicit_task_id(self, monkeypatch):
        from unittest.mock import MagicMock

        from app import tasks as tasks_module
        from app.pipeline.progress_types import StageProgress
        from app.tasks import _make_on_progress

        monkeypatch.setattr(tasks_module, "_publisher", MagicMock())

        fake_task = MagicMock()
        # Simula justo el escenario del bug: el contexto de Celery no está
        # disponible (como pasaría desde un thread aparte).
        fake_task.request.id = None

        on_progress = _make_on_progress(fake_task, "job123", "video")
        on_progress(StageProgress(stage="burning", stage_pct=50.0, message_key="status.burning_progress"))

        fake_task.update_state.assert_called_once()
        _, kwargs = fake_task.update_state.call_args
        assert kwargs.get("task_id") == "job123", (
            "update_state() tiene que recibir task_id explícito -- si no, depende de "
            "self.request.id, que puede ser None si se llama desde otro thread"
        )
