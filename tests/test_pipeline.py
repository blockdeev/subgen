"""Tests de integración de las etapas del pipeline (con I/O externo
mockeado: yt-dlp, deep-translator, ffmpeg, S3, Redis). Correr desde
`worker/`: `pytest ../tests/test_pipeline.py`."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import os
import subprocess
import tempfile

import pytest
import yt_dlp

from app.pipeline.burn import _build_style
from app.pipeline.download import DownloadError, TransientDownloadError, _classify_and_raise
from app.pipeline.errors import DeterministicPipelineError, TransientPipelineError
from app.pipeline.transcribe import Segment
from app.pipeline.translate import translate


# ── Clasificación de errores de descarga: determinístico vs. transitorio ──

class TestClassifyDownloadErrors:
    def test_invalid_url_is_deterministic(self):
        exc = yt_dlp.utils.DownloadError("Unsupported URL: not-a-real-url")
        with pytest.raises(DownloadError):
            _classify_and_raise(exc)

    def test_no_video_formats_is_deterministic(self):
        exc = yt_dlp.utils.DownloadError("Requested format is not available")
        with pytest.raises(DownloadError):
            _classify_and_raise(exc)

    def test_timeout_is_transient(self):
        exc = yt_dlp.utils.DownloadError("Connection timed out")
        with pytest.raises(TransientDownloadError):
            _classify_and_raise(exc)

    def test_connection_reset_is_transient(self):
        exc = yt_dlp.utils.DownloadError("Connection reset by peer")
        with pytest.raises(TransientDownloadError):
            _classify_and_raise(exc)

    def test_http_503_is_transient(self):
        exc = yt_dlp.utils.DownloadError("HTTP Error 503: Service Unavailable")
        with pytest.raises(TransientDownloadError):
            _classify_and_raise(exc)

    def test_non_ytdlp_exception_defaults_to_transient(self):
        # Cualquier excepción que no sea de yt-dlp (p.ej. un error de red
        # más bajo nivel) se trata como transitoria: preferimos reintentar
        # de más a fallar de más en un caso que no reconocemos.
        with pytest.raises(TransientDownloadError):
            _classify_and_raise(ConnectionError("boom"))

    def test_download_error_is_deterministic_pipeline_error(self):
        assert issubclass(DownloadError, DeterministicPipelineError)

    def test_transient_download_error_is_transient_pipeline_error(self):
        assert issubclass(TransientDownloadError, TransientPipelineError)


# ── _maybe_add_cookies: soporte de cookies.txt para YouTube ──────────────

class TestMaybeAddCookies:
    def test_no_cookies_file_leaves_opts_unchanged(self, tmp_path):
        from app.pipeline.download import _maybe_add_cookies

        opts = _maybe_add_cookies({"format": "best"}, None, tmp_path)
        assert "cookiefile" not in opts

    def test_empty_string_leaves_opts_unchanged(self, tmp_path):
        from app.pipeline.download import _maybe_add_cookies

        opts = _maybe_add_cookies({"format": "best"}, "", tmp_path)
        assert "cookiefile" not in opts

    def test_nonexistent_file_leaves_opts_unchanged(self, tmp_path):
        from app.pipeline.download import _maybe_add_cookies

        opts = _maybe_add_cookies({"format": "best"}, "/no/existe/cookies.txt", tmp_path)
        assert "cookiefile" not in opts

    def test_empty_file_leaves_opts_unchanged(self, tmp_path):
        # Es el caso del fallback /dev/null montado cuando no se configuró
        # ninguna cookie real: no debe pasarse a yt-dlp como si fuera válida.
        from app.pipeline.download import _maybe_add_cookies

        empty = tmp_path / "cookies_source.txt"
        empty.touch()
        opts = _maybe_add_cookies({"format": "best"}, str(empty), tmp_path)
        assert "cookiefile" not in opts

    def test_real_file_gets_copied_not_referenced_directly(self, tmp_path):
        """Este es EL test que hubiera agarrado el bug real: si en vez de
        copiar se pasa el path original tal cual, y ese original está
        montado read-only (:ro en el compose), yt-dlp explota con
        "Read-only file system" al intentar reescribir el cookiejar al
        terminar. Copiando a `writable_dir` se evita de raíz."""
        from app.pipeline.download import _maybe_add_cookies

        source_dir = tmp_path / "readonly_mount"
        source_dir.mkdir()
        source = source_dir / "cookies.txt"
        source.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tx\ty\n")

        writable_dir = tmp_path / "job_work_dir"
        writable_dir.mkdir()

        opts = _maybe_add_cookies({"format": "best"}, str(source), writable_dir)

        assert opts["cookiefile"] != str(source), (
            "no debe pasarle a yt-dlp el archivo original -- si ese mount "
            "es read-only, yt-dlp va a fallar al intentar reescribirlo"
        )
        assert Path(opts["cookiefile"]).parent == writable_dir
        assert Path(opts["cookiefile"]).read_text() == source.read_text()

    def test_copy_is_independent_of_the_original(self, tmp_path):
        # La copia tiene que ser un archivo de verdad, no un symlink al
        # original (un symlink a un mount :ro seguiría siendo read-only).
        from app.pipeline.download import _maybe_add_cookies

        source = tmp_path / "cookies.txt"
        source.write_text("original")
        writable_dir = tmp_path / "work"
        writable_dir.mkdir()

        opts = _maybe_add_cookies({"format": "best"}, str(source), writable_dir)

        copy_path = Path(opts["cookiefile"])
        assert not copy_path.is_symlink()
        copy_path.write_text("modificado")  # si esto fallara, seguiría siendo el mount :ro
        assert source.read_text() == "original"  # el original no se tocó

    def test_preserves_other_ydl_opts_keys(self, tmp_path):
        from app.pipeline.download import _maybe_add_cookies

        cookies = tmp_path / "cookies.txt"
        cookies.write_text("data")
        writable_dir = tmp_path / "work"
        writable_dir.mkdir()

        opts = _maybe_add_cookies({"format": "best", "quiet": True}, str(cookies), writable_dir)
        assert opts["format"] == "best"
        assert opts["quiet"] is True


# ── translate(): batch de 25, degradación silenciosa preservada ───────────

class TestTranslate:
    def _segments(self, n: int) -> list[Segment]:
        return [Segment(start=float(i), end=float(i + 1), text=f"hello {i}") for i in range(n)]

    def test_successful_batch_translation(self):
        fake_translator = MagicMock()
        fake_translator.translate_batch.return_value = ["hola 0", "hola 1"]
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate(self._segments(2), target_lang="es", batch_size=25)
        assert [s.text for s in result] == ["hola 0", "hola 1"]
        assert [s.text_original for s in result] == ["hello 0", "hello 1"]

    def test_batch_failure_falls_back_to_per_item(self):
        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = Exception("batch endpoint down")
        fake_translator.translate.side_effect = lambda t: t.replace("hello", "hola")
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate(self._segments(2), target_lang="es", batch_size=25)
        assert [s.text for s in result] == ["hola 0", "hola 1"]

    def test_item_failure_keeps_original_text(self):
        # Degradación intencional del original: si un ítem individual
        # también falla, se conserva el texto en inglés (no se rompe el job).
        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = Exception("down")
        fake_translator.translate.side_effect = Exception("also down")
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate(self._segments(1), target_lang="es", batch_size=25)
        assert result[0].text == "hello 0"  # se conservó el original

    def test_respects_batch_size(self):
        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = lambda texts: [t.upper() for t in texts]
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            translate(self._segments(60), target_lang="es", batch_size=25)
        call_sizes = [len(call.args[0]) for call in fake_translator.translate_batch.call_args_list]
        # Con concurrencia real, el ORDEN de ejecución no está garantizado
        # (los 3 lotes se someten a la vez a un pool de 4 workers) -- lo que
        # sí tiene que valer siempre es el conjunto de tamaños de lote.
        assert sorted(call_sizes) == [10, 25, 25]

    def test_empty_segments_returns_empty(self):
        assert translate([], target_lang="es") == []

    def test_translator_init_failure_is_transient(self):
        with patch("deep_translator.GoogleTranslator", side_effect=ConnectionError("dns fail")):
            with pytest.raises(TransientPipelineError):
                translate([Segment(start=0, end=1, text="hi")], target_lang="es")

    def test_order_preserved_even_when_later_batch_finishes_first(self):
        """El batch 0 tarda más que el batch 1 -- si el reensamblado
        dependiera del orden de FINALIZACIÓN en vez del índice del batch,
        esto rompería el orden de los segmentos en el resultado."""
        import time as time_module

        def slow_or_fast(texts):
            # Lote grande (batch 0, 25 items) se demora; lote chico
            # (batch 1, 5 items) termina primero.
            if len(texts) == 25:
                time_module.sleep(0.15)
            return [f"tr-{t}" for t in texts]

        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = slow_or_fast
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate(self._segments(30), target_lang="es", batch_size=25, max_concurrency=4)

        assert [s.text_original for s in result] == [f"hello {i}" for i in range(30)]
        assert result[0].text == "tr-hello 0"
        assert result[29].text == "tr-hello 29"

    def test_batches_run_concurrently_not_sequentially(self):
        """Con 4 lotes de 0.1s cada uno y concurrencia 4, el total tiene
        que acercarse a 0.1s (todos en paralelo), no a 0.4s (uno por uno)."""
        import time as time_module

        def slow_batch(texts):
            time_module.sleep(0.1)
            return texts

        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = slow_batch
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            t0 = time_module.monotonic()
            translate(self._segments(4), target_lang="es", batch_size=1, max_concurrency=4)
            elapsed = time_module.monotonic() - t0

        assert elapsed < 0.35, f"tardó {elapsed:.2f}s -- parece secuencial, no concurrente"

    def test_rate_limit_error_triggers_backoff_and_retry(self, monkeypatch):
        import app.pipeline.translate as translate_module

        sleeps: list[float] = []
        monkeypatch.setattr(translate_module.time, "sleep", lambda s: sleeps.append(s))

        fake_translator = MagicMock()
        # Falla las primeras 2 veces con algo que parece rate-limit, tercera vez ok.
        fake_translator.translate_batch.side_effect = [
            Exception("429 Too Many Requests"),
            Exception("429 Too Many Requests"),
            ["ok"],
        ]
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate([Segment(start=0, end=1, text="hi")], target_lang="es", batch_size=25)

        assert result[0].text == "ok"
        assert len(sleeps) == 2, "tendría que haber esperado antes del 2do y 3er intento"
        assert fake_translator.translate_batch.call_count == 3

    def test_non_rate_limit_error_does_not_retry_goes_straight_to_fallback(self, monkeypatch):
        import app.pipeline.translate as translate_module

        sleeps: list[float] = []
        monkeypatch.setattr(translate_module.time, "sleep", lambda s: sleeps.append(s))

        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = Exception("500 Internal Server Error")
        fake_translator.translate.side_effect = lambda t: f"item-{t}"
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate([Segment(start=0, end=1, text="hi")], target_lang="es", batch_size=25)

        assert result[0].text == "item-hi"
        assert sleeps == [], "un error que no parece rate-limit no debería esperar/reintentar el lote"
        assert fake_translator.translate_batch.call_count == 1

    def test_rate_limit_exhausts_retries_then_falls_back(self, monkeypatch):
        import app.pipeline.translate as translate_module

        monkeypatch.setattr(translate_module.time, "sleep", lambda s: None)

        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = Exception("429 Too Many Requests")
        fake_translator.translate.side_effect = lambda t: f"item-{t}"
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate([Segment(start=0, end=1, text="hi")], target_lang="es", batch_size=25)

        assert result[0].text == "item-hi"
        # 1 intento inicial + 3 reintentos = 4 llamadas a translate_batch antes de rendirse
        assert fake_translator.translate_batch.call_count == 4

    def test_translation_not_found_is_treated_as_retryable(self, monkeypatch):
        """El bug real que encontramos en vivo: bajo concurrencia, Google
        tira TranslationNotFound (no un 429), y antes eso NO se reconocía
        como rate-limit -- caía directo al inglés. Ahora se detecta por
        TIPO de excepción y se reintenta."""
        import app.pipeline.translate as translate_module
        from deep_translator.exceptions import TranslationNotFound

        monkeypatch.setattr(translate_module.time, "sleep", lambda s: None)

        fake_translator = MagicMock()
        # Primer intento del lote: TranslationNotFound. Segundo: bien.
        fake_translator.translate_batch.side_effect = [
            TranslationNotFound("frase --> No translation was found"),
            ["traducido ok"],
        ]
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate([Segment(start=0, end=1, text="hi")], target_lang="es", batch_size=25)

        assert result[0].text == "traducido ok", "TranslationNotFound debería reintentarse, no caer al inglés"
        assert fake_translator.translate_batch.call_count == 2

    def test_item_level_fallback_retries_each_item_not_just_gives_up(self, monkeypatch):
        """El segundo bug: cuando el lote cae al fallback ítem-por-ítem, ese
        fallback antes NO reintentaba -- un ítem que throttleaba quedaba en
        inglés aunque un reintento lo hubiera traducido. Ahora reintenta."""
        import app.pipeline.translate as translate_module
        from deep_translator.exceptions import TranslationNotFound

        monkeypatch.setattr(translate_module.time, "sleep", lambda s: None)

        fake_translator = MagicMock()
        # El lote entero falla siempre -> cae al fallback ítem por ítem.
        fake_translator.translate_batch.side_effect = TranslationNotFound("x")
        # En el fallback, el ítem falla las primeras 2 veces y a la 3ra anda.
        fake_translator.translate.side_effect = [
            TranslationNotFound("x"),
            TranslationNotFound("x"),
            "item traducido",
        ]
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate([Segment(start=0, end=1, text="hi")], target_lang="es", batch_size=25)

        assert result[0].text == "item traducido", "el ítem debería reintentarse en el fallback, no quedar en inglés"
        assert fake_translator.translate.call_count == 3

    def test_item_level_fallback_preserves_original_after_exhausting_retries(self, monkeypatch):
        import app.pipeline.translate as translate_module
        from deep_translator.exceptions import TranslationNotFound

        monkeypatch.setattr(translate_module.time, "sleep", lambda s: None)

        fake_translator = MagicMock()
        fake_translator.translate_batch.side_effect = TranslationNotFound("x")
        fake_translator.translate.side_effect = TranslationNotFound("x")  # falla siempre
        with patch("deep_translator.GoogleTranslator", return_value=fake_translator):
            result = translate([Segment(start=0, end=1, text="texto original")], target_lang="es", batch_size=25)

        # Tras agotar todos los reintentos, conserva el original (degradación intencional)
        assert result[0].text == "texto original"


# ── burn: el estilo quemado tiene que ser EXACTAMENTE el validado ─────────

def test_burn_style_matches_validated_original():
    settings = SimpleNamespace(
        burn_font_name="Arial", burn_font_size=20,
        burn_primary_colour="&HFFFFFF&", burn_outline_colour="&H000000&",
        burn_border_style=1, burn_outline=2, burn_shadow=1, burn_margin_v=25,
    )
    style = _build_style(settings)
    assert style == (
        "FontName=Arial,FontSize=20,PrimaryColour=&HFFFFFF&,"
        "OutlineColour=&H000000&,BorderStyle=1,Outline=2,Shadow=1,MarginV=25"
    )


class TestBurnSubsOutputFps:
    """El filtro `fps=N` tiene que ir ANTES de `subtitles=` en la cadena de
    -vf, no como opción de salida separada -- si no, libass sigue
    procesando todos los frames del source y no se ahorra nada."""

    def _settings(self):
        return SimpleNamespace(
            burn_font_name="Arial", burn_font_size=20,
            burn_primary_colour="&HFFFFFF&", burn_outline_colour="&H000000&",
            burn_border_style=1, burn_outline=2, burn_shadow=1, burn_margin_v=25,
            ffmpeg_preset="fast", ffmpeg_crf=23, ffmpeg_timeout_seconds=1800,
        )

    def _captured_vf(self, monkeypatch, tmp_path, output_fps):
        import app.pipeline.burn as burn_module

        captured = {}

        def fake_run(cmd, total_seconds, timeout_seconds, on_progress):
            captured["cmd"] = cmd
            (tmp_path / "out.mp4").write_bytes(b"x" * 2000)  # burn_subs valida tamaño > 1000
            return 0, ""

        monkeypatch.setattr(burn_module, "probe_duration_seconds", lambda p: 10.0)
        monkeypatch.setattr(burn_module, "_run_ffmpeg_with_progress", fake_run)

        burn_module.burn_subs(
            tmp_path / "in.mp4", tmp_path / "in.srt", tmp_path / "out.mp4",
            settings=self._settings(), output_fps=output_fps,
        )
        vf_index = captured["cmd"].index("-vf") + 1
        return captured["cmd"][vf_index]

    def test_fps_filter_comes_before_subtitles_filter(self, monkeypatch, tmp_path):
        vf = self._captured_vf(monkeypatch, tmp_path, output_fps=30)
        assert vf.startswith("fps=30,subtitles="), vf

    def test_fps_60_in_filter_chain(self, monkeypatch, tmp_path):
        vf = self._captured_vf(monkeypatch, tmp_path, output_fps=60)
        assert vf.startswith("fps=60,subtitles="), vf

    def test_zero_or_negative_fps_omits_the_filter_entirely(self, monkeypatch, tmp_path):
        vf = self._captured_vf(monkeypatch, tmp_path, output_fps=0)
        assert "fps=" not in vf
        assert vf.startswith("subtitles="), vf


# ── process_video: caminos de error de punta a punta, con I/O mockeado ────

@pytest.fixture
def task_env(monkeypatch, tmp_path):
    """Evita que la tarea toque Redis/S3 reales durante el test, y usa un
    work_dir descartable en vez de /tmp/subgen."""
    from app import tasks as tasks_module

    monkeypatch.setattr(tasks_module._publisher, "publish", MagicMock())
    monkeypatch.setattr(tasks_module.process_video, "update_state", lambda **kw: None)
    monkeypatch.setattr(tasks_module, "upload_file", lambda *a, **kw: "fake-key")
    monkeypatch.setattr(tasks_module.settings, "work_dir", str(tmp_path / "work"))
    # Default: sí tiene audio. TestNoAudioStream lo pisa explícitamente
    # cuando necesita probar el caso contrario.
    monkeypatch.setattr(tasks_module, "has_audio_stream", lambda path: True)
    return tasks_module


def test_no_speech_detected_raises_deterministic_error(task_env, tmp_path, monkeypatch):
    from app.pipeline.download import DownloadResult

    monkeypatch.setattr(
        task_env, "download_audio_only",
        lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=10.0),
    )
    monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [])  # sin habla detectada

    with pytest.raises(DeterministicPipelineError, match="No se detectó habla"):
        task_env.process_video.apply(args=("job1", "https://example.com/v", "es", "srt")).get()


def test_video_exceeding_max_duration_is_rejected(task_env, tmp_path, monkeypatch):
    from app.pipeline.download import DownloadResult

    monkeypatch.setattr(task_env.settings, "max_video_duration_seconds", 60)
    monkeypatch.setattr(
        task_env, "download_audio_only",
        lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=999.0),
    )

    with pytest.raises(DeterministicPipelineError, match="supera el máximo"):
        task_env.process_video.apply(args=("job2", "https://example.com/v", "es", "srt")).get()


def test_ffmpeg_failure_is_not_retried(task_env, tmp_path, monkeypatch):
    from app.pipeline.burn import BurnError
    from app.pipeline.download import DownloadResult
    from app.pipeline.translate import TranslatedSegment

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake video bytes")

    monkeypatch.setattr(
        task_env, "download_video_full",
        lambda *a, **kw: (DownloadResult(path=video_path, title="t", duration=10.0), tmp_path / "audio.mp3"),
    )
    monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
    monkeypatch.setattr(
        task_env, "translate",
        lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
    )
    monkeypatch.setattr(task_env, "burn_subs", MagicMock(side_effect=BurnError("ffmpeg crashed")))

    with pytest.raises(BurnError):
        task_env.process_video.apply(args=("job3", "https://example.com/v", "es", "video")).get()


class TestOutputFpsPropagation:
    """El request puede elegir 30/60fps; si no manda nada (output_fps=0,
    el default del parámetro), tiene que caer al default del server."""

    def _setup_video_mode(self, task_env, tmp_path, monkeypatch):
        from app.pipeline.download import DownloadResult
        from app.pipeline.translate import TranslatedSegment

        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"fake video bytes")
        monkeypatch.setattr(
            task_env, "download_video_full",
            lambda *a, **kw: (DownloadResult(path=video_path, title="t", duration=10.0), tmp_path / "audio.mp3"),
        )
        monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
        monkeypatch.setattr(
            task_env, "translate",
            lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
        )
        burn_mock = MagicMock(side_effect=lambda video_path, srt_path, output_path, **kw: output_path.write_bytes(b"x" * 100) or output_path)
        monkeypatch.setattr(task_env, "burn_subs", burn_mock)
        return burn_mock

    def test_explicit_60fps_reaches_burn_subs(self, task_env, tmp_path, monkeypatch):
        burn_mock = self._setup_video_mode(task_env, tmp_path, monkeypatch)
        result = task_env.process_video.apply(
            args=("job11", "https://example.com/v", "es", "video", 60)
        ).get()
        assert burn_mock.call_args.kwargs["output_fps"] == 60
        assert result["output_fps"] == 60

    def test_zero_falls_back_to_server_default(self, task_env, tmp_path, monkeypatch):
        burn_mock = self._setup_video_mode(task_env, tmp_path, monkeypatch)
        monkeypatch.setattr(task_env.settings, "default_burn_fps", 30)
        result = task_env.process_video.apply(
            args=("job12", "https://example.com/v", "es", "video", 0)
        ).get()
        assert burn_mock.call_args.kwargs["output_fps"] == 30
        assert result["output_fps"] == 30

    def test_omitted_argument_also_falls_back_to_default(self, task_env, tmp_path, monkeypatch):
        # Ni siquiera se manda el 5to argumento -- confirma que las tareas
        # viejas (tests existentes con 4 args) siguen andando igual.
        burn_mock = self._setup_video_mode(task_env, tmp_path, monkeypatch)
        monkeypatch.setattr(task_env.settings, "default_burn_fps", 30)
        result = task_env.process_video.apply(
            args=("job13", "https://example.com/v", "es", "video")
        ).get()
        assert burn_mock.call_args.kwargs["output_fps"] == 30
        assert result["output_fps"] == 30


def test_successful_srt_only_run_returns_expected_shape(task_env, tmp_path, monkeypatch):
    from app.pipeline.download import DownloadResult
    from app.pipeline.translate import TranslatedSegment

    monkeypatch.setattr(
        task_env, "download_audio_only",
        lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="Mi Video", duration=10.0),
    )
    monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
    monkeypatch.setattr(
        task_env, "translate",
        lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
    )

    result = task_env.process_video.apply(args=("job4", "https://example.com/v", "es", "srt")).get()

    assert result["title"] == "Mi Video"
    assert result["mode"] == "srt"
    assert result["segments_count"] == 1
    assert "video_key" not in result
    assert result["srt_key"].startswith("job4/")


# ── download._make_hook: adapta el progress_hook de yt-dlp, sin red real ──

class TestDownloadProgressHook:
    def test_downloading_status_reports_percentage(self):
        from app.pipeline.download import _make_hook
        from app.pipeline.progress_types import StageProgress

        events: list[StageProgress] = []
        hook = _make_hook("job", events.append)
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "eta": 30})

        assert len(events) == 1
        assert events[0].stage == "downloading"
        assert events[0].stage_pct == pytest.approx(25.0)
        assert events[0].eta_seconds == 30.0

    def test_downloading_status_without_total_bytes_reports_zero(self):
        from app.pipeline.download import _make_hook

        events = []
        hook = _make_hook("job", events.append)
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 0})
        assert events[0].stage_pct == 0.0

    def test_finished_status_reports_100_percent(self):
        from app.pipeline.download import _make_hook

        events = []
        hook = _make_hook("job", events.append)
        hook({"status": "finished"})
        assert events[0].stage_pct == 100.0

    def test_percentage_is_capped_below_100_while_downloading(self):
        # Nunca reporta 100% en "downloading": el 100% real lo manda el
        # evento "finished" (yt-dlp todavía puede tener postprocesadores
        # corriendo después de "downloaded_bytes == total_bytes").
        from app.pipeline.download import _make_hook

        events = []
        hook = _make_hook("job", events.append)
        hook({"status": "downloading", "downloaded_bytes": 200, "total_bytes": 200})
        assert events[0].stage_pct <= 99.0


# ── transcribe(): con el modelo de Whisper mockeado, sin cargar nada real ─

class TestTranscribe:
    def _fake_segment(self, start, end, text):
        return SimpleNamespace(start=start, end=end, text=text)

    def test_reports_progress_based_on_audio_duration(self, monkeypatch, tmp_path):
        import app.pipeline.transcribe as transcribe_module

        raw_segments = [self._fake_segment(i, i + 1, f"seg {i}") for i in range(10)]
        fake_model = MagicMock()
        fake_model.transcribe.return_value = (raw_segments, SimpleNamespace(language="en"))
        monkeypatch.setattr(transcribe_module, "get_whisper_model", lambda *a, **kw: fake_model)

        events = []
        result = transcribe_module.transcribe(
            tmp_path / "audio.mp3",
            model_name="small", device="cpu", compute_type="int8",
            audio_duration_seconds=10.0, on_progress=events.append,
        )

        assert len(result) == 10
        assert result[0].text == "seg 0"
        final_progress_events = [e for e in events if e.stage_pct == pytest.approx(100.0)]
        assert final_progress_events  # terminó reportando 100%

    def test_passes_configured_whisper_parameters(self, monkeypatch, tmp_path):
        import app.pipeline.transcribe as transcribe_module

        fake_model = MagicMock()
        fake_model.transcribe.return_value = ([], SimpleNamespace(language="en"))
        monkeypatch.setattr(transcribe_module, "get_whisper_model", lambda *a, **kw: fake_model)

        transcribe_module.transcribe(
            tmp_path / "audio.mp3", model_name="small", device="cpu", compute_type="int8",
            beam_size=7, vad_min_silence_ms=444, vad_speech_pad_ms=111, audio_duration_seconds=5.0,
        )

        _, kwargs = fake_model.transcribe.call_args
        assert kwargs["beam_size"] == 7
        assert kwargs["language"] == "en"
        assert kwargs["vad_filter"] is True
        assert kwargs["vad_parameters"] == dict(min_silence_duration_ms=444, speech_pad_ms=111)


# ── burn: helpers auxiliares con subprocess mockeado ───────────────────────

class TestBurnHelpers:
    def test_escape_srt_path_handles_colons_and_quotes(self):
        from app.pipeline.burn import _escape_srt_path

        result = _escape_srt_path(Path("C:/videos/it's a test.srt"))
        assert "\\:" in result
        assert "\\'" in result

    def test_probe_duration_parses_ffprobe_output(self, monkeypatch):
        from app.pipeline import burn as burn_module

        fake_result = SimpleNamespace(stdout="123.456000\n", returncode=0)
        monkeypatch.setattr(burn_module.subprocess, "run", lambda *a, **kw: fake_result)
        assert burn_module.probe_duration_seconds(Path("video.mp4")) == pytest.approx(123.456)

    def test_probe_duration_returns_zero_on_bad_output(self, monkeypatch):
        from app.pipeline import burn as burn_module

        fake_result = SimpleNamespace(stdout="N/A\n", returncode=1)
        monkeypatch.setattr(burn_module.subprocess, "run", lambda *a, **kw: fake_result)
        assert burn_module.probe_duration_seconds(Path("video.mp4")) == 0.0


class TestFFmpegPipeHandling:
    """Test real, con ffmpeg de verdad (sin mocks): antes, `_run_ffmpeg_with_progress`
    usaba `process.communicate()` mientras un thread aparte ya estaba
    leyendo `process.stdout` de forma independiente para el progreso — dos
    consumidores sobre el mismo pipe al mismo tiempo, comportamiento
    indefinido con riesgo real de colgarse. Este test genera más stderr
    que el buffer de pipe típico de Linux (64KB) a propósito, para probar
    bajo carga real que el fix (un thread dedicado por cada pipe) no se
    cuelga ni pierde datos.
    """

    def test_does_not_hang_with_large_stderr_output(self):
        import shutil as shutil_module

        if shutil_module.which("ffmpeg") is None:
            pytest.skip("ffmpeg no disponible en este entorno")

        from app.pipeline.burn import _run_ffmpeg_with_progress

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            cmd = [
                "ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=25:size=640x480:rate=25",
                "-loglevel", "debug",  # a propósito: genera MUCHO más stderr que lo normal
                "-c:v", "libx264", "-preset", "ultrafast", "-y",
                "-progress", "pipe:1", "-nostats",
                str(output),
            ]
            events: list = []
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_seconds=25.0, timeout_seconds=90, on_progress=events.append,
            )
            output_exists = output.exists()
            output_size = output.stat().st_size if output_exists else 0

        assert returncode == 0
        assert output_exists and output_size > 0
        assert len(stderr) > 64 * 1024, (
            f"solo se capturaron {len(stderr)} bytes de stderr -- si el pipe se "
            "hubiera llenado y bloqueado como en el bug original, esto sería "
            "mucho menor (truncado) en vez de completo"
        )
        # El progreso también se reportó bien mientras tanto -- confirma que
        # ambos pipes (stdout de progreso Y stderr) se drenaron en paralelo
        # sin pisarse.
        assert len(events) > 0

    def test_kills_orphaned_process_on_unexpected_interruption(self, monkeypatch):
        """Bug real encontrado en producción: `SoftTimeLimitExceeded` de
        Celery (1700s) es MENOR al timeout propio de FFmpeg (1800s), así
        que en la práctica siempre se dispara primero -- e interrumpe
        `process.wait()` sin pasar por nuestro `except
        subprocess.TimeoutExpired`. Sin el fix, el proceso de FFmpeg
        quedaba corriendo huérfano en el contenedor para siempre. Acá
        simulamos esa interrupción externa con un proceso real."""
        import shutil as shutil_module

        if shutil_module.which("ffmpeg") is None:
            pytest.skip("ffmpeg no disponible en este entorno")

        from app.pipeline.burn import _run_ffmpeg_with_progress

        captured: dict = {}
        original_wait = subprocess.Popen.wait
        call_count = {"n": 0}

        def failing_wait(self, *a, **kw):
            captured["proc"] = self
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simula SoftTimeLimitExceeded interrumpiendo a mitad de
                # camino -- una excepción que NO es subprocess.TimeoutExpired.
                raise RuntimeError("interrupción externa simulada")
            return original_wait(self, *a, **kw)

        monkeypatch.setattr(subprocess.Popen, "wait", failing_wait)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            cmd = [
                "ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=30:size=320x240:rate=10",
                "-c:v", "libx264", "-preset", "ultrafast", "-y",
                "-progress", "pipe:1", "-nostats",
                str(output),
            ]
            with pytest.raises(RuntimeError):
                _run_ffmpeg_with_progress(
                    cmd, total_seconds=30.0, timeout_seconds=60, on_progress=lambda e: None,
                )

        proc = captured["proc"]
        assert proc.poll() is not None, "el proceso de ffmpeg quedó huérfano corriendo"


class TestHasAudioStream:
    def test_true_when_ffprobe_lists_a_stream(self, monkeypatch):
        from app.pipeline import download as download_module

        fake_result = SimpleNamespace(stdout="0\n", returncode=0)
        monkeypatch.setattr(download_module.subprocess, "run", lambda *a, **kw: fake_result)
        assert download_module.has_audio_stream(Path("video.mp4")) is True

    def test_false_when_ffprobe_output_is_empty(self, monkeypatch):
        # Este es el caso real que causaba el IndexError de PyAV: un video
        # mudo, sin ningún stream de audio.
        from app.pipeline import download as download_module

        fake_result = SimpleNamespace(stdout="", returncode=0)
        monkeypatch.setattr(download_module.subprocess, "run", lambda *a, **kw: fake_result)
        assert download_module.has_audio_stream(Path("video.mp4")) is False

    def test_fails_open_on_ffprobe_timeout(self, monkeypatch):
        import subprocess as subprocess_module

        from app.pipeline import download as download_module

        def raise_timeout(*a, **kw):
            raise subprocess_module.TimeoutExpired(cmd="ffprobe", timeout=30)

        monkeypatch.setattr(download_module.subprocess, "run", raise_timeout)
        # No bloqueamos el pipeline por un timeout de ffprobe: se asume que
        # sí tiene audio y que si no, ya va a fallar más adelante con un
        # error más claro.
        assert download_module.has_audio_stream(Path("video.mp4")) is True


# ── storage.py: boto3 client mockeado, sin red real ────────────────────────

class TestStorage:
    def _settings(self):
        return SimpleNamespace(
            s3_endpoint_url="http://minio:9000", s3_access_key="k", s3_secret_key="s",
            s3_bucket="subgen-outputs", s3_region="us-east-1", s3_use_ssl=False,
        )

    def test_upload_file_calls_boto_with_correct_bucket_and_key(self, tmp_path, monkeypatch):
        from app import storage as storage_module

        local_file = tmp_path / "out.srt"
        local_file.write_text("contenido")
        fake_client = MagicMock()
        monkeypatch.setattr(storage_module, "get_client", lambda settings: fake_client)

        key = storage_module.upload_file(self._settings(), local_file, "job1/out.srt")

        assert key == "job1/out.srt"
        fake_client.upload_file.assert_called_once_with(str(local_file), "subgen-outputs", "job1/out.srt")

    def test_upload_file_wraps_client_errors(self, tmp_path, monkeypatch):
        from botocore.exceptions import ClientError

        from app import storage as storage_module
        from app.storage import StorageError

        fake_client = MagicMock()
        fake_client.upload_file.side_effect = ClientError({"Error": {"Code": "500", "Message": "x"}}, "PutObject")
        monkeypatch.setattr(storage_module, "get_client", lambda settings: fake_client)

        with pytest.raises(StorageError):
            storage_module.upload_file(self._settings(), tmp_path / "f.srt", "k")

    def test_list_objects_older_than_filters_by_last_modified(self, monkeypatch):
        import datetime as dt

        from app import storage as storage_module

        now = dt.datetime.now(dt.timezone.utc)
        old_obj = {"Key": "old/file.srt", "LastModified": now - dt.timedelta(hours=48)}
        new_obj = {"Key": "new/file.srt", "LastModified": now - dt.timedelta(hours=1)}

        fake_paginator = MagicMock()
        fake_paginator.paginate.return_value = [{"Contents": [old_obj, new_obj]}]
        fake_client = MagicMock()
        fake_client.get_paginator.return_value = fake_paginator
        monkeypatch.setattr(storage_module, "get_client", lambda settings: fake_client)

        stale = storage_module.list_objects_older_than(self._settings(), prefix="", max_age_hours=24)
        assert stale == ["old/file.srt"]

    def test_object_exists_true_and_false(self, monkeypatch):
        from botocore.exceptions import ClientError

        from app import storage as storage_module

        fake_client = MagicMock()
        monkeypatch.setattr(storage_module, "get_client", lambda settings: fake_client)
        assert storage_module.object_exists(self._settings(), "k") is True

        fake_client.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "x"}}, "HeadObject")
        assert storage_module.object_exists(self._settings(), "k") is False


# ── progress.py: publisher de Redis Pub/Sub, con el cliente mockeado ──────

class TestProgressPublisher:
    def test_channel_name_format(self):
        from app.progress import channel_name

        assert channel_name("abc123") == "progress:abc123"

    def test_publish_serializes_payload_as_json(self, monkeypatch):
        import json

        from app import progress as progress_module

        fake_redis_client = MagicMock()
        monkeypatch.setattr(progress_module.redis, "from_url", lambda *a, **kw: fake_redis_client)

        publisher = progress_module.ProgressPublisher("redis://fake:6379/0")
        publisher.publish("job1", {"status": "downloading", "progress": 42})

        channel, payload = fake_redis_client.publish.call_args.args
        assert channel == "progress:job1"
        assert json.loads(payload) == {"status": "downloading", "progress": 42}


# ── tasks.py: rutas de excepción que todavía no tenían cobertura ─────────

class TestCleanupStaleWorkdirs:
    """Consecuencia directa de agregar el cancelar-job con SIGKILL: ese
    kill no le da chance a nuestro `finally` de correr, así que el
    work_dir del job cancelado queda huérfano en el disco del worker.
    Esto lo barre al arrancar CADA job nuevo (no es tarea de Beat porque
    cada worker tiene su propio filesystem local, ver docstring real)."""

    def test_removes_old_dirs_but_keeps_current_job_and_recent_ones(self, task_env, tmp_path, monkeypatch):
        import os
        import time as time_module

        work_root = tmp_path / "work_root"
        work_root.mkdir()
        monkeypatch.setattr(task_env.settings, "work_dir", str(work_root))
        monkeypatch.setattr(task_env.settings, "cleanup_max_age_hours", 1)

        old_dir = work_root / "old-job-huerfano"
        old_dir.mkdir()
        recent_dir = work_root / "recent-job"
        recent_dir.mkdir()
        current_dir = work_root / "current-job"
        current_dir.mkdir()

        old_ts = time_module.time() - (2 * 3600)  # 2 horas -- más viejo que el límite de 1h
        os.utime(old_dir, (old_ts, old_ts))

        task_env._cleanup_stale_workdirs("current-job")

        assert not old_dir.exists(), "el directorio viejo huérfano debería haberse borrado"
        assert recent_dir.exists(), "el directorio reciente no debería tocarse"
        assert current_dir.exists(), "el directorio del job actual nunca se toca, aunque sea viejo"

    def test_no_op_if_work_dir_does_not_exist_yet(self, task_env, tmp_path, monkeypatch):
        monkeypatch.setattr(task_env.settings, "work_dir", str(tmp_path / "no-existe-todavia"))
        task_env._cleanup_stale_workdirs("cualquier-job")  # no debe explotar

    def test_ignores_files_only_touches_directories(self, task_env, tmp_path, monkeypatch):
        import os
        import time as time_module

        work_root = tmp_path / "work_root"
        work_root.mkdir()
        monkeypatch.setattr(task_env.settings, "work_dir", str(work_root))
        monkeypatch.setattr(task_env.settings, "cleanup_max_age_hours", 1)

        stray_file = work_root / "no-es-un-directorio.txt"
        stray_file.write_text("x")
        old_ts = time_module.time() - (2 * 3600)
        os.utime(stray_file, (old_ts, old_ts))

        task_env._cleanup_stale_workdirs("current-job")  # no debe explotar ni tocar el archivo

        assert stray_file.exists()

    def test_called_at_the_start_of_process_video(self, task_env, tmp_path, monkeypatch):
        from app.pipeline.download import DownloadResult
        from app.pipeline.translate import TranslatedSegment

        calls = []
        monkeypatch.setattr(task_env, "_cleanup_stale_workdirs", lambda job_id: calls.append(job_id))
        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=1.0),
        )
        monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
        monkeypatch.setattr(
            task_env, "translate",
            lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
        )

        task_env.process_video.apply(args=("job-cleanup-check", "https://example.com/v", "es", "srt")).get()

        assert calls == ["job-cleanup-check"]


class TestWorkDirCleanedUpOnFailure:
    """El bloqueante de espacio en disco pedía verificar que la limpieza
    cubre fallos y cancelaciones, no solo el camino feliz. El `finally`
    de process_video corre para CUALQUIER excepción (está a nivel del
    try más externo) -- estos tests lo confirman con ejecución real, no
    solo leyendo el código."""

    def test_workdir_removed_after_deterministic_error(self, task_env, tmp_path, monkeypatch):
        monkeypatch.setattr(task_env, "download_audio_only", lambda *a, **kw: (_ for _ in ()).throw(
            DeterministicPipelineError("boom")))

        job_id = "job-fail-det"
        with pytest.raises(DeterministicPipelineError):
            task_env.process_video.apply(args=(job_id, "https://example.com/v", "es", "srt")).get()

        assert not (tmp_path / "work" / job_id).exists(), "el work_dir debería haberse borrado igual, aunque el job falló"

    def test_workdir_removed_after_unexpected_exception(self, task_env, tmp_path, monkeypatch):
        monkeypatch.setattr(task_env, "download_audio_only", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("algo totalmente inesperado")))

        job_id = "job-fail-unexpected"
        with pytest.raises(RuntimeError):
            task_env.process_video.apply(args=(job_id, "https://example.com/v", "es", "srt")).get()

        assert not (tmp_path / "work" / job_id).exists(), "incluso una excepción no prevista tiene que limpiar el work_dir"

    def test_workdir_removed_after_success_too(self, task_env, tmp_path, monkeypatch):
        from app.pipeline.download import DownloadResult
        from app.pipeline.translate import TranslatedSegment

        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=1.0),
        )
        monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
        monkeypatch.setattr(
            task_env, "translate",
            lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
        )

        job_id = "job-success"
        task_env.process_video.apply(args=(job_id, "https://example.com/v", "es", "srt")).get()

        assert not (tmp_path / "work" / job_id).exists()


class TestDiskSpaceCheck:
    """Chequeo pre-vuelo: falla RÁPIDO si no hay margen de disco, en vez
    de romperse a mitad de un burn largo."""

    def test_raises_deterministic_error_when_space_is_low(self, task_env, monkeypatch):
        import shutil as shutil_module
        from unittest.mock import MagicMock as MM

        fake_usage = MM(free=100 * 1024 * 1024)  # 100MB libres
        monkeypatch.setattr(shutil_module, "disk_usage", lambda path: fake_usage)
        monkeypatch.setattr(task_env.settings, "min_free_disk_mb", 3072)

        with pytest.raises(DeterministicPipelineError, match="[Ee]spacio en disco"):
            task_env._check_disk_space(3072)

    def test_passes_when_space_is_sufficient(self, task_env, monkeypatch):
        import shutil as shutil_module
        from unittest.mock import MagicMock as MM

        fake_usage = MM(free=10 * 1024 * 1024 * 1024)  # 10GB libres
        monkeypatch.setattr(shutil_module, "disk_usage", lambda path: fake_usage)

        task_env._check_disk_space(3072)  # no debe lanzar nada

    def test_zero_or_negative_disables_the_check(self, task_env, monkeypatch):
        import shutil as shutil_module

        def boom(path):
            raise AssertionError("no debería llamarse a disk_usage con el chequeo deshabilitado")

        monkeypatch.setattr(shutil_module, "disk_usage", boom)
        task_env._check_disk_space(0)
        task_env._check_disk_space(-1)

    def test_low_disk_space_fails_the_job_without_retry(self, task_env, tmp_path, monkeypatch):
        """Confirma que el chequeo, enganchado adentro de process_video,
        efectivamente frena el job ANTES de arrancar download -- no solo
        que la función aislada lanza la excepción."""
        import shutil as shutil_module
        from unittest.mock import MagicMock as MM

        fake_usage = MM(free=1 * 1024 * 1024)  # 1MB libres
        monkeypatch.setattr(shutil_module, "disk_usage", lambda path: fake_usage)
        monkeypatch.setattr(task_env.settings, "min_free_disk_mb", 3072)

        download_calls = []
        monkeypatch.setattr(task_env, "download_audio_only", lambda *a, **kw: download_calls.append(1))

        with pytest.raises(DeterministicPipelineError, match="[Ee]spacio en disco"):
            task_env.process_video.apply(args=("job-disk-full", "https://example.com/v", "es", "srt")).get()

        assert download_calls == [], "no debería haber arrancado la descarga si no hay espacio"


class TestTaskExceptionPaths:

    def test_storage_error_is_not_retried(self, task_env, tmp_path, monkeypatch):
        from app.pipeline.download import DownloadResult
        from app.pipeline.translate import TranslatedSegment
        from app.storage import StorageError

        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=10.0),
        )
        monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
        monkeypatch.setattr(
            task_env, "translate",
            lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
        )
        monkeypatch.setattr(task_env, "upload_file", MagicMock(side_effect=StorageError("bucket unreachable")))

        with pytest.raises(StorageError):
            task_env.process_video.apply(args=("job5", "https://example.com/v", "es", "srt")).get()

    def test_soft_time_limit_exceeded_is_logged_and_reraised(self, task_env, monkeypatch):
        from celery.exceptions import SoftTimeLimitExceeded

        monkeypatch.setattr(
            task_env, "download_audio_only",
            MagicMock(side_effect=SoftTimeLimitExceeded()),
        )

        with pytest.raises(SoftTimeLimitExceeded):
            task_env.process_video.apply(args=("job6", "https://example.com/v", "es", "srt")).get()

    def test_cleanup_task_deletes_stale_objects(self, task_env, monkeypatch):
        monkeypatch.setattr(task_env, "list_objects_older_than", lambda *a, **kw: ["a/1.srt", "b/2.mp4"])
        deleted = []
        monkeypatch.setattr(task_env, "delete_object", lambda settings, key: deleted.append(key))

        count = task_env.cleanup_expired_outputs.apply().get()

        assert count == 2
        assert deleted == ["a/1.srt", "b/2.mp4"]


class TestNoAudioStream:
    def test_video_without_audio_raises_deterministic_error_with_clear_message(
        self, task_env, tmp_path, monkeypatch
    ):
        """Caso real: un video mudo hace fallar la extracción de audio, el
        fallback usa el video directo, y ANTES esto llegaba crudo a Whisper
        y explotaba con un IndexError de PyAV que no matcheaba ningún except
        -> la tarea fallaba en Celery pero el frontend nunca se enteraba."""
        from app.pipeline.download import DownloadResult

        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "video_sin_audio.mp4", title="t", duration=10.0),
        )
        monkeypatch.setattr(task_env, "has_audio_stream", lambda path: False)

        with pytest.raises(DeterministicPipelineError, match="no tiene pista de audio"):
            task_env.process_video.apply(args=("job7", "https://example.com/v", "es", "srt")).get()

    def test_video_with_audio_stream_proceeds_normally(self, task_env, tmp_path, monkeypatch):
        from app.pipeline.download import DownloadResult
        from app.pipeline.translate import TranslatedSegment

        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=10.0),
        )
        monkeypatch.setattr(task_env, "has_audio_stream", lambda path: True)
        monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
        monkeypatch.setattr(
            task_env, "translate",
            lambda *a, **kw: [TranslatedSegment(start=0, end=1, text_original="hi", text="hola")],
        )

        result = task_env.process_video.apply(args=("job8", "https://example.com/v", "es", "srt")).get()
        assert result["segments_count"] == 1


class TestUnexpectedExceptionSafetyNet:
    def test_unclassified_exception_still_publishes_terminal_error(self, task_env, tmp_path, monkeypatch):
        """La red de seguridad: una excepción que no es ninguna de nuestras
        clases custom (acá simulamos el IndexError real de PyAV) tiene que
        terminar publicando un evento 'error' igual, para que el frontend
        no se quede colgado esperando un mensaje que nunca llega."""
        from app.pipeline.download import DownloadResult

        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=10.0),
        )
        monkeypatch.setattr(task_env, "has_audio_stream", lambda path: True)
        monkeypatch.setattr(task_env, "transcribe", MagicMock(side_effect=IndexError("tuple index out of range")))

        with pytest.raises(IndexError):
            task_env.process_video.apply(args=("job9", "https://example.com/v", "es", "srt")).get()

        published_calls = task_env._publisher.publish.call_args_list
        assert published_calls, "nunca se publicó nada al frontend"
        job_id, payload = published_calls[-1].args
        assert job_id == "job9"
        assert payload["status"] == "error"
        assert "tuple index out of range" in payload["message_params"]["detail"]


class TestTransientRetryExhaustion:
    """Este es el bug real que apareció con las cookies de YouTube: un
    OSError "Read-only file system" al copiar mal el cookiefile se
    clasificaba como transitorio y Celery reintentaba la tarea ENTERA
    (incluida la descarga) una y otra vez -- pero como el error nunca iba a
    dejar de pasar, terminaba en un bucle silencioso donde el frontend
    jamás se enteraba de que en el fondo ya había fallado para siempre.
    """

    def test_publishes_terminal_error_only_after_retries_exhausted(self, task_env, tmp_path, monkeypatch):
        from app.pipeline.download import DownloadResult

        monkeypatch.setattr(
            task_env, "download_audio_only",
            lambda *a, **kw: DownloadResult(path=tmp_path / "audio.mp3", title="t", duration=1.0),
        )
        monkeypatch.setattr(task_env, "has_audio_stream", lambda path: True)
        monkeypatch.setattr(task_env, "transcribe", lambda *a, **kw: [Segment(start=0, end=1, text="hi")])
        monkeypatch.setattr(
            task_env, "translate",
            MagicMock(side_effect=TransientPipelineError("boom")),
        )
        # max_retries=1 y sin backoff real: agota rápido, sin esperar de
        # verdad los segundos de backoff configurados.
        monkeypatch.setattr(task_env.process_video, "max_retries", 1)
        monkeypatch.setattr(task_env.process_video, "retry_backoff", False)

        with pytest.raises(TransientPipelineError):
            task_env.process_video.apply(args=("job10", "https://example.com/v", "es", "srt")).get()

        # Durante el primer intento (retries=0 < max_retries=1) NO debe
        # haber publicado nada todavía -- recién en el último intento
        # agotado. Si esto publicara en cada intento, el usuario vería
        # "error" en pantalla mientras el job en realidad seguía
        # reintentando en segundo plano.
        published_calls = task_env._publisher.publish.call_args_list
        assert len(published_calls) == 1, (
            f"se publicó {len(published_calls)} veces, esperaba exactamente 1 "
            "(solo en el intento final agotado)"
        )
        job_id, payload = published_calls[0].args
        assert job_id == "job10"
        assert payload["status"] == "error"
        assert "boom" in payload["message_params"]["detail"]


class TestCpuThreadsAndBatchedInference:
    """Verificado contra la API real de faster-whisper instalada
    (inspeccioné WhisperModel.__init__ y BatchedInferencePipeline.transcribe
    con inspect.signature antes de escribir esto) -- pero sin poder cargar
    un modelo real acá (huggingface.co no está en la allowlist de red de
    este sandbox), así que todo esto usa mocks. La comparación real de
    calidad/timestamps entre secuencial y batched queda pendiente de
    correr en hardware real, ver README."""

    def setup_method(self):
        import app.pipeline.transcribe as transcribe_module
        transcribe_module._model = None
        transcribe_module._batched_pipeline = None

    def teardown_method(self):
        import app.pipeline.transcribe as transcribe_module
        transcribe_module._model = None
        transcribe_module._batched_pipeline = None

    def test_resolve_cpu_threads_uses_configured_value_when_positive(self):
        from app.pipeline.transcribe import resolve_cpu_threads
        assert resolve_cpu_threads(8) == 8

    def test_resolve_cpu_threads_falls_back_to_available_cores_when_zero(self):
        from app.pipeline.transcribe import resolve_cpu_threads
        result = resolve_cpu_threads(0)
        assert result > 0
        assert result == len(os.sched_getaffinity(0))

    def test_whisper_model_constructed_with_resolved_cpu_threads(self, monkeypatch):
        import app.pipeline.transcribe as transcribe_module

        captured = {}

        class FakeWhisperModel:
            def __init__(self, model_name, device, compute_type, cpu_threads):
                captured["cpu_threads"] = cpu_threads

        monkeypatch.setattr(
            "faster_whisper.WhisperModel", FakeWhisperModel,
        )
        transcribe_module.get_whisper_model("small", "cpu", "int8", cpu_threads=6)
        assert captured["cpu_threads"] == 6

    def test_sequential_path_forces_without_timestamps_false(self, monkeypatch, tmp_path):
        import app.pipeline.transcribe as transcribe_module

        captured = {}

        class FakeModel:
            def transcribe(self, path, **kwargs):
                captured.update(kwargs)
                return [], SimpleNamespace(language="en")

        transcribe_module._model = FakeModel()

        transcribe_module.transcribe(
            tmp_path / "audio.mp3", model_name="small", device="cpu", compute_type="int8",
            use_batched=False,
        )
        assert captured["without_timestamps"] is False

    def test_batched_path_forces_without_timestamps_false_and_passes_batch_size(self, monkeypatch, tmp_path):
        """El caso que importa de verdad: BatchedInferencePipeline.transcribe
        tiene without_timestamps=True por DEFAULT en la librería -- si esto
        no lo pisara explícito, se rompería el pipeline entero en silencio."""
        import app.pipeline.transcribe as transcribe_module

        captured = {}

        class FakeBatchedPipeline:
            def transcribe(self, path, **kwargs):
                captured.update(kwargs)
                return [], SimpleNamespace(language="en")

        transcribe_module._batched_pipeline = FakeBatchedPipeline()

        transcribe_module.transcribe(
            tmp_path / "audio.mp3", model_name="small", device="cpu", compute_type="int8",
            use_batched=True, batch_size=16,
        )
        assert captured["without_timestamps"] is False
        assert captured["batch_size"] == 16

    def test_batched_pipeline_reuses_the_same_model_singleton_not_double_loaded(self, monkeypatch):
        import app.pipeline.transcribe as transcribe_module

        load_count = {"n": 0}

        class FakeWhisperModel:
            def __init__(self, *a, **kw):
                load_count["n"] += 1

        class FakeBatchedInferencePipeline:
            def __init__(self, model):
                self.model = model

        monkeypatch.setattr("faster_whisper.WhisperModel", FakeWhisperModel)
        monkeypatch.setattr("faster_whisper.BatchedInferencePipeline", FakeBatchedInferencePipeline)

        transcribe_module.get_batched_pipeline("small", "cpu", "int8")

        assert load_count["n"] == 1, "el modelo se cargó más de una vez"

    def test_non_batched_mode_never_touches_batched_pipeline(self, tmp_path):
        """Con use_batched=False (el default), get_batched_pipeline no
        debería ni llamarse -- no tiene sentido armar el pipeline batched
        si no se va a usar."""
        import app.pipeline.transcribe as transcribe_module

        class FakeModel:
            def transcribe(self, path, **kwargs):
                return [], SimpleNamespace(language="en")

        transcribe_module._model = FakeModel()

        def fail_if_called(*a, **kw):
            raise AssertionError("get_batched_pipeline no debería llamarse con use_batched=False")

        transcribe_module.get_batched_pipeline = fail_if_called  # type: ignore[assignment]
        try:
            transcribe_module.transcribe(
                tmp_path / "audio.mp3", model_name="small", device="cpu", compute_type="int8",
                use_batched=False,
            )
        finally:
            import importlib
            importlib.reload(transcribe_module)


class TestResolveMaxTasksPerChild:
    """Mitigación de threads huérfanos (worker_max_tasks_per_child) --
    ver ARCHITECTURE.md 'Problemas conocidos' para el contexto completo."""

    def test_positive_value_used_as_is(self):
        from app.celery_app import resolve_max_tasks_per_child
        assert resolve_max_tasks_per_child(1) == 1
        assert resolve_max_tasks_per_child(5) == 5

    def test_zero_or_negative_disables_the_mitigation(self):
        from app.celery_app import resolve_max_tasks_per_child
        assert resolve_max_tasks_per_child(0) is None
        assert resolve_max_tasks_per_child(-1) is None

    def test_default_setting_applies_to_the_real_celery_conf(self):
        """No solo la función pura -- confirma que el valor realmente
        llega al conf de la app de Celery, no que se quede sin usar."""
        from app.celery_app import celery_app
        assert celery_app.conf.worker_max_tasks_per_child == 1
