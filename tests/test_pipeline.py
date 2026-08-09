"""Tests de integración de las etapas del pipeline (con I/O externo
mockeado: yt-dlp, deep-translator, ffmpeg, S3, Redis). Correr desde
`worker/`: `pytest ../tests/test_pipeline.py`."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        assert call_sizes == [25, 25, 10]

    def test_empty_segments_returns_empty(self):
        assert translate([], target_lang="es") == []

    def test_translator_init_failure_is_transient(self):
        with patch("deep_translator.GoogleTranslator", side_effect=ConnectionError("dns fail")):
            with pytest.raises(TransientPipelineError):
                translate([Segment(start=0, end=1, text="hi")], target_lang="es")


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
