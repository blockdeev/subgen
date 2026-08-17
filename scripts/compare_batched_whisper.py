#!/usr/bin/env python3
"""Compara transcripción SECUENCIAL vs BATCHED de faster-whisper sobre el
MISMO audio real, y escribe los dos .srt resultantes + un reporte de
diferencias -- para correr donde el modelo Whisper ya está cacheado (el
sandbox de Claude no tiene acceso a huggingface.co, así que esto nunca se
corrió contra un modelo real; el código de transcribe.py sí está
verificado contra la firma real de la librería instalada, pero la
comparación de CALIDAD queda pendiente de este script).

Uso, DENTRO del contenedor worker (tiene el modelo ya cacheado en el
volumen whisper-cache):

    docker cp scripts/compare_batched_whisper.py subgen-worker-1-1:/tmp/
    docker cp /ruta/a/tu/audio.mp3 subgen-worker-1-1:/tmp/audio.mp3
    docker exec -it subgen-worker-1-1 python /tmp/compare_batched_whisper.py \
        /tmp/audio.mp3 --model small --compute-type int8 --batch-size 8

O localmente si tenés faster-whisper instalado con el modelo ya bajado:

    python compare_batched_whisper.py audio.mp3 --model small --compute-type int8

Qué mirar en la salida (ver también el reporte que imprime al final):

1. CANTIDAD DE SEGMENTOS: van a diferir, y no es necesariamente un
   problema -- el modo batched segmenta por CHUNK DE VAD (más grueso,
   frases más largas por segmento), el secuencial segmenta por PAUSA
   NATURAL detectada en los tokens de timestamp (más fino). Si batched da
   MENOS segmentos pero más largos, es el comportamiento esperado de la
   arquitectura, no necesariamente una degradación.

2. COBERTURA TEMPORAL: el 'último timestamp' de los dos caminos tiene que
   estar cerca (unos pocos segundos de diferencia como mucho). Si batched
   corta mucho antes que secuencial, ahí SÍ hay un problema real -- perdió
   contenido.

3. TEXTO TRANSCRITO: leé unos cuantos segmentos de cada .srt uno al lado
   del otro (no hace falta los 300, con 15-20 salteados a lo largo del
   video alcanza). Lo que importa es si el CONTENIDO coincide, no que el
   corte de frases sea idéntico -- una frase que en secuencial son 2
   segmentos y en batched es 1 solo (con el mismo texto combinado) es
   una diferencia de SEGMENTACIÓN, no de calidad.

4. PALABRAS TOTALES: el conteo de palabras de los dos .srt completos
   debería ser parecido (dentro de un 5-10%). Una diferencia grande ahí sí
   es señal de que uno de los dos caminos está perdiendo contenido real.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def format_srt_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(segments: list[tuple[float, float, str]], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(segments, 1):
            f.write(f"{i}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{text}\n\n")


def run_sequential(model, audio_path: str) -> tuple[list[tuple[float, float, str]], float]:
    t0 = time.monotonic()
    segs, info = model.transcribe(
        audio_path, language="en", task="transcribe", beam_size=5,
        word_timestamps=False, without_timestamps=False,
        vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
    )
    result = [(s.start, s.end, s.text.strip()) for s in segs]
    elapsed = time.monotonic() - t0
    print(f"[secuencial] idioma detectado: {info.language} (prob={info.language_probability:.2f})")
    return result, elapsed


def run_batched(model, audio_path: str, batch_size: int) -> tuple[list[tuple[float, float, str]], float]:
    from faster_whisper import BatchedInferencePipeline

    pipeline = BatchedInferencePipeline(model=model)
    t0 = time.monotonic()
    segs, info = pipeline.transcribe(
        audio_path, language="en", task="transcribe", beam_size=5,
        word_timestamps=False, without_timestamps=False,  # ver nota en transcribe.py: sin esto se rompe en silencio
        vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        batch_size=batch_size,
    )
    result = [(s.start, s.end, s.text.strip()) for s in segs]
    elapsed = time.monotonic() - t0
    print(f"[batched]     idioma detectado: {info.language} (prob={info.language_probability:.2f})")
    return result, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio_path", help="Ruta al archivo de audio (mismo que uses para comparar)")
    parser.add_argument("--model", default="small", help="Tamaño del modelo Whisper (default: small, el que usa SubGen)")
    parser.add_argument("--compute-type", default="int8", help="default: int8 (el que usa SubGen)")
    parser.add_argument("--cpu-threads", type=int, default=0, help="0 = detectar cores disponibles")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--out-dir", default=".", help="Dónde escribir los .srt de salida")
    args = parser.parse_args()

    if not Path(args.audio_path).exists():
        print(f"No existe el archivo: {args.audio_path}", file=sys.stderr)
        return 1

    from faster_whisper import WhisperModel

    print(f"Cargando modelo '{args.model}' (compute_type={args.compute_type})...")
    model = WhisperModel(args.model, device="cpu", compute_type=args.compute_type, cpu_threads=args.cpu_threads)

    print("\n=== Corriendo transcripción SECUENCIAL (el pipeline actual de SubGen) ===")
    seq_segments, seq_time = run_sequential(model, args.audio_path)
    print(f"{len(seq_segments)} segmentos en {seq_time:.1f}s")

    print(f"\n=== Corriendo transcripción BATCHED (batch_size={args.batch_size}) ===")
    batch_segments, batch_time = run_batched(model, args.audio_path, args.batch_size)
    print(f"{len(batch_segments)} segmentos en {batch_time:.1f}s")

    out_dir = Path(args.out_dir)
    seq_srt = out_dir / "secuencial.srt"
    batch_srt = out_dir / "batched.srt"
    write_srt(seq_segments, seq_srt)
    write_srt(batch_segments, batch_srt)

    seq_words = sum(len(text.split()) for _, _, text in seq_segments)
    batch_words = sum(len(text.split()) for _, _, text in batch_segments)
    seq_last = seq_segments[-1][1] if seq_segments else 0.0
    batch_last = batch_segments[-1][1] if batch_segments else 0.0

    print("\n" + "=" * 60)
    print("REPORTE")
    print("=" * 60)
    print(f"{'':20}{'secuencial':>15}{'batched':>15}{'diferencia':>15}")
    print(f"{'tiempo':20}{seq_time:>14.1f}s{batch_time:>14.1f}s{f'{(1 - batch_time/seq_time)*100:+.0f}%' if seq_time else 'n/a':>15}")
    print(f"{'segmentos':20}{len(seq_segments):>15}{len(batch_segments):>15}{len(batch_segments) - len(seq_segments):>+15}")
    print(f"{'palabras totales':20}{seq_words:>15}{batch_words:>15}{f'{(batch_words/seq_words - 1)*100:+.1f}%' if seq_words else 'n/a':>15}")
    print(f"{'último timestamp':20}{seq_last:>14.1f}s{batch_last:>14.1f}s{f'{batch_last - seq_last:+.1f}s':>15}")
    print()
    print(f".srt escritos en: {seq_srt} / {batch_srt}")
    print()
    if abs(batch_last - seq_last) > 10:
        print("ALERTA: los timestamps finales difieren por más de 10s -- revisar cobertura, puede haber contenido perdido.")
    if seq_words and abs(batch_words / seq_words - 1) > 0.10:
        print("ALERTA: el conteo de palabras difiere más del 10% -- revisar calidad del texto transcrito, no solo timing.")
    print("Ver la guía completa de qué mirar en el docstring de este script.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
