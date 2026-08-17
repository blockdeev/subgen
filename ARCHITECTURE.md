# Quemado distribuido por segmentos — diseño completo, pendiente de datos de producción

Este documento existe porque el trabajo de diseño ya se hizo, se validó
en partes con evidencia real, y se decidió **no implementarlo todavía** --
no por falta de mérito técnico, sino porque las proyecciones de ganancia
disponibles tenían un margen de error demasiado grande para justificar la
complejidad que agrega. Queda documentado completo para retomarlo con
datos de producción reales.

## El problema que resuelve

Hoy, un job entero (descarga, transcripción, traducción, quemado) corre
de punta a punta en **un solo worker**. Con dos o más workers, eso reduce
la latencia entre jobs *distintos* corriendo en paralelo (throughput),
pero no reduce el tiempo de **un job individual** -- ese sigue atado a la
capacidad de una sola VPS.

La etapa de quemado (`burn`) es la más larga del pipeline (en las
mediciones de desarrollo, entre 45% y 58% del tiempo total del job según
la configuración), y tiene una característica particular: el filtro
`subtitles` de FFmpeg (que usa `libass` para componer los subtítulos
sobre cada frame) es **mono-hilo** -- no importa cuántos cores le des a
`x264` para codificar, `libass` sigue usando uno solo.

## La idea: partir el video en N segmentos, quemarlos en paralelo, unirlos

En vez de un solo proceso de FFmpeg quemando el video entero, partirlo en
N pedazos, quemar cada uno en un proceso **separado** (potencialmente en
workers/VPS distintos), y unirlos al final. Esto no es solo repartir el
trabajo del encoder entre más cores -- **también paraleliza `libass`**,
porque cada segmento corre su propia instancia mono-hilo, pero N
instancias corriendo en paralelo hacen N veces el trabajo de composición
de subtítulos en el mismo tiempo de reloj. Es la forma correcta de
esquivar la limitación de un solo hilo de `libass`, en vez de intentar
hacer que la librería en sí sea multi-hilo (no lo es, y no lo va a ser).

## Diseño técnico

### Pre-corte del source (stream-copy, no re-encode)

En vez de que cada worker descargue el video completo (N× el tamaño de
transferencia si hay N workers en VPS distintas), el video se pre-corta
**una sola vez**, con `ffmpeg -f segment -c copy` -- stream-copy puro,
sin decodificar/codificar, así que es prácticamente instantáneo
comparado con un re-encode.

**Punto crítico verificado con datos reales, no asumido**: como es
stream-copy, cada corte cae en el **keyframe más cercano** al punto
pedido, no en el punto exacto. Corrí el experimento real: pedí cortes
cada 7 segundos sobre un source con keyframe cada 2 segundos, y las
piezas reales dieron `[8, 6, 8, 6, 2]` segundos -- nunca los 7
solicitados. Por eso las duraciones reales de cada pieza se miden con
`ffprobe` **después** de cortar, nunca se asumen los offsets planeados.
Esto ya está implementado y testeado en `worker/app/pipeline/segments.py`
(`boundaries_from_durations`).

### Recorte de cues en las fronteras

Un subtítulo que cruza el punto de corte (ej. de 9:58 a 10:02, con el
corte en 10:00) tiene que aparecer **recortado en los dos segmentos** que
toca, no completo en uno solo ni descartado. Esto también está
implementado y testeado (`split_cues_by_segment` en `segments.py`,
con el caso exacto de arriba como test explícito) -- los timestamps de
salida ya quedan en tiempo LOCAL de cada segmento, listos para pasarle
directo a `make_srt`.

### Cada segmento, en paralelo

Por cada segmento: descargar su pieza pre-cortada desde S3 (no el video
completo), quemar sus subtítulos (ya recortados y con timestamps
locales), con:

- **Audio recodificado, no copiado** (`-c:a copy` → recodificar a AAC
  consistente): el pre-corte por stream-copy no garantiza que el audio
  caiga en un límite de frame exacto, así que copiarlo podría dejar un
  click/glitch casi imperceptible en cada unión. Recodificar cuesta poco
  (audio es barato comparado con video) y elimina el riesgo. Decisión ya
  tomada y aprobada.
- **Presupuesto de threads explícito** (`-threads <cores_totales / N>`):
  sin esto, N segmentos en paralelo sobre-suscriben CPU y no dan la
  ganancia esperada -- cada proceso pelea por los mismos cores en vez de
  sumar trabajo útil.

Cada segmento sube su resultado quemado a S3 y reporta su progreso local
vía Redis Pub/Sub (agregado con los demás en un hash `progress:{job_id}:segments`
para el porcentaje global, ver diseño de progreso más abajo).

### Orquestación: chord de Celery, con una corrección importante

`chord(group(burn_segment.s(...) for i in range(N)), concat_segments.s(...))`
-- el callback de concatenación solo corre si **todos** los segmentos
terminaron bien.

**Verificado en vivo, con Redis real, matando una subtarea a propósito**:
`link_error` en un `group` de Celery no se puede enganchar al grupo
completo -- Celery lo rechaza explícitamente ("Cannot add link to group:
do that on individual tasks"), hay que engancharlo tarea por tarea. Y más
importante: **el callback de error se dispara apenas falla el primer
hermano, sin esperar a que terminen los demás** -- confirmado con logs
reales: `error_callback_fired` apareció ANTES de que los segmentos sanos
terminaran de subir su resultado. Un diseño de limpieza que borre "los
segmentos que ya subieron" en ese momento tiene una condición de carrera
real: en el momento del error, los hermanos sanos pueden no haber subido
nada todavía.

**Diseño de cleanup corregido**: el error callback NO intenta limpieza
precisa. Marca el job como fallido de inmediato (feedback rápido al
usuario, que es lo que importa) y deja que la tarea periódica de limpieza
que ya existe (`cleanup_expired_outputs`, corre por
`SUBGEN_CLEANUP_INTERVAL_SECONDS`) barra los segmentos huérfanos por
prefijo, sin ninguna condición de carrera porque no hay nada sincrónico
peleándole el paso a un hermano que todavía está subiendo.

### Progreso agregado

Cada segmento publica su progreso local (0-100% de sí mismo). El
porcentaje global de la etapa "burning" es el promedio de los N
progresos locales, mantenido en un hash de Redis (`progress:{job_id}:segments`,
un campo por índice de segmento) -- cada actualización de un segmento
recalcula el promedio y lo publica por el mismo canal de Pub/Sub que ya
usa el resto del pipeline, sin tocar el frontend.

### Camino N=1: sin cambios

Con `SUBGEN_BURN_PARALLEL_SEGMENTS=1` (o sin configurar), el código tiene
que tomar el camino actual, exacto, sin pasar por S3 staging, sin chord,
sin nada de lo de arriba -- la complejidad nueva es 100% opt-in.

## Por qué quedó pendiente, con números concretos

La ganancia proyectada cambió varias veces a medida que se corrigieron
los supuestos, y ese historial importa más que el número final:

1. Primera estimación (dentro de una sola VPS de 4 cores, usando la curva
   de threads real medida): **1.34x** en el mejor caso.
2. Corrección: el diseño real reparte entre las DOS VPS de worker, no
   dentro de una sola -- con la curva real (que satura fuerte después de
   8-16 threads), la mejor combinación entre 2 VPS de 4 cores cada una da
   **~2.7x**.
3. Traducido a tiempo total del job (no solo la etapa de burn): con la
   ganancia optimista de 2.7x aplicada solo a `burn`, el ahorro sobre el
   job completo es de **~13%** -- porque `transcribe` y `translate` no se
   tocan con este diseño, y siguen siendo una porción grande del total.
4. Ese 13% optimista **no incluye** el overhead real de staging en S3,
   orquestación del chord, ni la contención entre procesos concurrentes
   compitiendo por los mismos cores (que las mediciones aisladas no
   capturan).

Mientras tanto, dos cambios mucho más baratos (forzar h264 en el
selector de descarga, y bajar el output a 30fps) dieron **29% de
reducción real, medida de punta a punta**, sin ninguno de los riesgos de
arriba. Ese resultado es la razón concreta de la postergación: no tiene
sentido comprometerse a la complejidad de un sistema distribuido por una
ganancia proyectada menor a la que ya se consiguió con dos cambios de
una línea.

## Qué hace falta para retomar la decisión

Un número real de producción, no una extrapolación desde una máquina de
desarrollo de 32 cores. Con la VPS de worker real ya desplegada (ver
`DEPLOYMENT.md`, sección "Medir en producción"), corriendo un video largo
real:

- Si el tiempo de `burn` en producción escala como predice la curva
  local (ajustada al core count real de la VPS), el ahorro de este diseño
  seguiría rondando ese ~13% optimista -- probablemente no justifica la
  complejidad.
- Si el hardware de producción muestra un comportamiento peor al
  proyectado (por steal time en CPX, por ejemplo), el diseño de
  segmentos se vuelve más atractivo, porque paraleliza también `libass` y
  no depende de que un solo proceso escale bien en ese hardware
  específico.

## Qué queda implementado, listo para retomar

- `worker/app/pipeline/segments.py`: `boundaries_from_durations` y
  `split_cues_by_segment`, ambas puras y testeadas (14 tests en
  `tests/test_segments.py`, incluido el caso explícito de cue cruzando
  frontera).
- Este documento, como diseño completo de `burn_segment`,
  `concat_segments`, y el manejo de errores/cleanup -- nada de esto está
  implementado en `tasks.py` todavía, es la especificación para cuando se
  retome.

---

# Problemas conocidos (independiente del diseño de arriba)

Documentado acá porque es el mismo tipo de decisión que el resto de este
archivo: un problema real, con una mitigación aplicada, pendiente de
diagnóstico de causa raíz cuando haya tiempo -- no bloqueante para
producción, pero tampoco resuelto del todo.

## Threads huérfanos de ctranslate2/Whisper

**Síntoma observado en vivo, más de una vez**: un worker queda
consumiendo 800%+ de CPU sostenido, sin ninguna tarea activa según
`celery -A app.celery_app inspect active` (confirmado con dos fuentes
coincidiendo: la UI sin ningún job visible, y Celery mismo reportando
`- empty -` en los dos nodos). El proceso responsable NO es un `ffmpeg`
separado (ya conocíamos ese patrón, distinto) -- es el mismo PID del
worker de Celery, con el conteo de threads/PIDs de `docker stats` muy por
encima de lo esperable para `concurrency=1`.

**Hipótesis, sin confirmar todavía**: un pool de threads interno de
`ctranslate2` (el motor detrás de `faster-whisper`) que sobrevive a una
interrupción fuerte de la tarea que lo inició -- un `SIGKILL` externo
(cancelación de job, `docker compose kill`, un cuelgue del host) no le da
chance a Python de correr limpieza, y a diferencia de un subproceso
(`ffmpeg`, que si se le manda `kill` muere entero), un thread-pool
*dentro* del mismo proceso puede seguir vivo aunque la tarea de Celery
que lo lanzó ya haya sido abandonada. No investigado a fondo -- es una
hipótesis razonable, no una causa confirmada.

**Mitigación aplicada (no arregla la causa raíz):**
`SUBGEN_CELERY_MAX_TASKS_PER_CHILD` (default `1`), que se traduce en
`worker_max_tasks_per_child=1` de Celery. Con `concurrency=1`, esto
fuerza que el pool de Celery recicle el proceso del worker después de
CADA tarea -- cualquier thread huérfano que haya quedado vivo dentro de
ese proceso muere junto con él, sin excepción, porque el sistema operativo
se encarga de matar el proceso entero, no algo que dependa de que nuestro
código Python se entere.

**Trade-off, medido**: recargar el modelo Whisper cuesta ~1.5s (medido en
los logs reales de esta sesión, línea "Modelo Whisper cargado"). Contra
un job que tarda decenas de minutos a horas (transcripción + quemado de
un video largo), esto es completamente despreciable -- no hay razón para
NO tener esto activado en producción. Queda configurable
(`SUBGEN_CELERY_MAX_TASKS_PER_CHILD=0` deshabilita) solo por si en algún
escenario de desarrollo/debugging puntual se quiere el comportamiento
viejo (proceso persistente entre tareas, más rápido de iterar).

**Qué falta para cerrar esto de verdad** (postergado explícitamente,
retomar con tiempo dedicado, no en medio de otra cosa):
- Reproducir el síntoma de forma controlada (no solo haberlo visto
  aparecer en medio de sesiones de debugging con muchas cancelaciones
  encadenadas).
- Confirmar o descartar la hipótesis del thread-pool de `ctranslate2`
  específicamente -- por ejemplo, inspeccionando con `py-spy` o similar
  qué está corriendo de verdad dentro del proceso cuando aparece el
  síntoma, en vez de inferirlo indirectamente.
- Si se confirma, ver si `ctranslate2`/`faster-whisper` expone algún
  mecanismo de cierre explícito del pool de threads que se pueda
  enganchar en un `finally`, en vez de depender de reciclar el proceso
  entero.

**Nota**: no es el único caso de "algo puede sobrevivir a la tarea que lo
lanzó" en este proyecto -- el burn de FFmpeg tiene el mismo problema de
fondo con su propio subproceso (ver el `finally` de
`_run_ffmpeg_with_progress` en `worker/app/pipeline/burn.py`, que sí
tiene un `kill()` explícito para ESE caso puntual). La diferencia es que
un subproceso se puede matar desde afuera con `kill()`; un thread-pool
*dentro* del mismo proceso Python no tiene un equivalente tan directo --
por eso la mitigación acá es reciclar el proceso entero, no un kill
puntual.
