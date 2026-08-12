# SubGen

Genera subtitulos en espanol (o el idioma que elijas) para videos en ingles
de cualquier sitio soportado por [yt-dlp](https://github.com/yt-dlp/yt-dlp),
usando [faster-whisper](https://github.com/SYSTRAN/faster-whisper) para
transcribir y Google Translate para traducir. Dos modos: solo el archivo
`.srt`, o el video completo con los subtitulos quemados.

Esta es la version migrada de un monolito Flask + threads a una
arquitectura distribuida: **FastAPI + Celery + Redis**, containerizada,
pensada para correr en 4 VPS separados (o en una sola maquina para
desarrollo/demo).

```
+--------------+     +-------------+     +--------------+     +--------------+
|   Frontend   |---->|   FastAPI   |---->|    Redis     |<--->|   Celery     |
|  (estatico,  |<----|    (API)    |<----|  (broker +   |     |   Workers    |
|  servido por | WS  |             |     |   backend)   |     |  (Whisper +  |
|   la propia  |     |             |     |              |     |   FFmpeg)    |
|    API)      |     +------+------+     +--------------+     +------+-------+
+--------------+            |                                        |
     VPS 1                  |            +--------------+            |
                             +----------->|  S3-compat.  |<-----------+
                                          |   storage    |
                                          | (MinIO/S3)   |
                                          +--------------+
   VPS 1              VPS 1                  VPS 2                VPS 3 y 4
 (frontend)            (api)                (redis)          (worker-1, worker-2)
```

El worker sube el resultado (`.srt` y, en modo video, el `.mp4`) a un bucket
S3-compatible; la API nunca toca el filesystem del worker, solo genera URLs
pre-firmadas contra ese mismo bucket. Ver [Almacenamiento](#almacenamiento)
para el porque.

---

## Indice

- [Levantar en local](#levantar-en-local)
- [Variables de entorno](#variables-de-entorno)
- [Cookies de YouTube](#cookies-de-youtube)
- [Despliegue distribuido en 4 VPS de Hetzner](#despliegue-distribuido-en-4-vps-de-hetzner)
- [Correr los tests](#correr-los-tests)
- [Decisiones de arquitectura](#decisiones-de-arquitectura)
- [Seguridad](#seguridad)
- [Checklist de verificacion manual](#checklist-de-verificacion-manual)

---

## Levantar en local

Requisitos: Docker y Docker Compose v2.

```bash
cp .env.example .env
docker compose up --build
```

Esto levanta 5 contenedores: `redis`, `minio` (stand-in local del
almacenamiento S3), `api`, `worker-1` (con Celery Beat embebido) y
`worker-2`. La app queda en **http://localhost:8000**. La consola web de
MinIO queda en **http://localhost:9001** (usuario/clave: `subgen` /
`subgen12345`, configurables en `.env`).

Primera vez: la descarga del modelo Whisper `small` puede tardar unos
minutos (se cachea en la imagen del worker en el primer uso, no en cada
arranque de contenedor gracias al volumen implicito de Docker).

Para bajar todo: `docker compose down -v` (el `-v` borra los volumenes de
Redis y MinIO -- sacalo si queres conservar los datos entre corridas).

---

## Variables de entorno

Todas con prefijo `SUBGEN_`. Ver `.env.example` para los defaults completos.

| Variable | Servicio(s) | Descripcion |
|---|---|---|
| `REDIS_URL` | api, worker | Broker + result backend de Celery |
| `CELERY_TASK_TIME_LIMIT` / `_SOFT_TIME_LIMIT` | worker | Limite duro/blando por tarea (segundos) |
| `CELERY_MAX_RETRIES` / `_RETRY_BACKOFF` / `_RETRY_BACKOFF_MAX` | worker | Politica de reintentos para fallos transitorios |
| `CELERY_TASK_ALWAYS_EAGER` | api | Solo para tests de integracion |
| `WHISPER_MODEL` | worker | Modelo de faster-whisper (`small` por default, igual al original) |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` | worker | `cpu`/`int8` por default -- ver nota abajo |
| `WHISPER_BEAM_SIZE`, `WHISPER_VAD_*` | worker | Parametros de transcripcion (iguales al original) |
| `TRANSLATE_BATCH_SIZE` | worker | Tamano de lote de traduccion (25, igual al original) |
| `MAX_VIDEO_DURATION_SECONDS` | api, worker | Limite de duracion aceptada (0 = sin limite) |
| `BURN_*`, `FFMPEG_*` | worker | Estilo de subtitulos quemados y flags de FFmpeg -- **no tocar**, es el estilo validado |
| `S3_ENDPOINT_URL` | api, worker | Endpoint interno del bucket S3-compatible |
| `S3_PUBLIC_ENDPOINT_URL` | api | Endpoint que ve el navegador del usuario (para presigned URLs) |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` / `S3_REGION` / `S3_USE_SSL` | api, worker | Credenciales y bucket |
| `S3_PRESIGNED_URL_EXPIRY_SECONDS` | api | Vigencia de los links de descarga |
| `CLEANUP_MAX_AGE_HOURS` / `CLEANUP_INTERVAL_SECONDS` | worker | Limpieza automatica (Celery Beat) |
| `CORS_ORIGINS` | api | Lista separada por comas. **Nunca `*` en produccion** |
| `RATE_LIMIT_CREATE_JOB` | api | Sintaxis `N/minute`, `N/hour` (slowapi) |
| `MAX_URL_LENGTH` | api | Validacion de la URL de entrada |
| `SERVE_FRONTEND` / `FRONTEND_DIR` | api | La API sirve el frontend estatico (igual que el Flask original) |
| `LOG_LEVEL` | api, worker | Nivel de logging (JSON estructurado) |
| `YTDLP_COOKIES_FILE` | worker | Ruta DENTRO del contenedor al cookies.txt (fija, no tocar) |

> **Nota sobre `WHISPER_DEVICE`/`WHISPER_COMPUTE_TYPE`**: los defaults
> (`cpu`/`int8`) asumen VPS de Hetzner sin GPU, que es lo tipico en sus
> planes estandar. Si desplegas en una VPS con GPU, cambia a `cuda` y un
> `compute_type` como `float16`.

---

## Cookies de YouTube

YouTube viene bloqueando cada vez mas la descarga sin sesion con un error
tipo `"Sign in to confirm you're not a bot"` -- le pasa a `yt-dlp` sin
cookies, mas seguido todavia desde IPs de datacenter/VPS. No es un bug de
esta app: el `app.py` original de Flask tenia exactamente el mismo
problema potencial (usaba yt-dlp sin cookies tambien), solo que la
deteccion de YouTube se volvio mas agresiva con el tiempo.

**Como exportar tus cookies** (elegi una):

- Extension de navegador tipo "Get cookies.txt LOCALLY" (Chrome/Firefox):
  entra a youtube.com logueado, exporta en formato Netscape, guardalo como
  `cookies.txt`.
- O con el propio yt-dlp instalado en tu máquina (no en el contenedor):
  `yt-dlp --cookies-from-browser chrome --cookies cookies.txt --skip-download "https://youtube.com/watch?v=dQw4w9WgXcQ"`
  (cualquier URL sirve, el flag `--skip-download` solo exporta las
  cookies).

**Como configurarlo:**

```bash
# En .env (o en el .env de la VPS correspondiente si es despliegue distribuido):
YTDLP_COOKIES_FILE_HOST=/ruta/absoluta/a/tu/cookies.txt

docker compose up -d --build worker-1 worker-2
```

Si `YTDLP_COOKIES_FILE_HOST` queda vacío, `docker-compose.yml` monta
`/dev/null` en su lugar -- yt-dlp simplemente no usa cookies, exactamente
el comportamiento de antes. No hace falta nada más para que la app siga
funcionando con sitios que no bloquean por bots.

**Importante:**

- `cookies.txt` son credenciales de sesión reales. Ya está en `.gitignore`
  -- **nunca lo commitees**.
- Las cookies expiran. Si después de un tiempo vuelve a aparecer el error
  de bot, repetí la exportación.
- Solo el worker necesita esto (es quien corre `yt-dlp`); la API no lo usa
  para nada.

---

## Despliegue distribuido en 4 VPS de Hetzner

Docker Compose no orquesta multi-host: la forma de desplegar "distribuido"
con compose solo (sin Kubernetes/Swarm, como pide el enunciado) es copiar
el mismo `docker-compose.prod.yml` a las 4 VPS y, en cada una, levantar
**unicamente** el servicio que le corresponde.

### 1. Crear la red privada

En el panel de Hetzner Cloud: **Networks -> Create Network**, rango
`10.0.0.0/24` por ejemplo. Agrega las 4 VPS a esa red al crearlas (o
despues, en la seccion Networks de cada servidor). Cada VPS va a tener una
IP privada tipo `10.0.0.X` ademas de su IP publica.

Anota las IPs privadas -- las vas a necesitar en el `.env` de cada nodo:

| VPS | Rol | IP privada (ejemplo) |
|---|---|---|
| VPS 1 | `api` | `10.0.0.11` |
| VPS 2 | `redis` (+ `minio` si elegis self-hosted) | `10.0.0.12` |
| VPS 3 | `worker-1` | `10.0.0.13` |
| VPS 4 | `worker-2` | `10.0.0.14` |

### 2. Instalar Docker en las 4

```bash
curl -fsSL https://get.docker.com | sh
```

### 3. Copiar el repo a las 4

```bash
git clone <tu-repo> subgen && cd subgen
```

(Solo hace falta el codigo fuente para el build; no hace falta copiar
`node_modules` ni nada pesado -- `.dockerignore` ya se encarga.)

### 4. Configurar el `.env` de cada VPS

Cada VPS necesita su **propio** `.env`, distinto entre si. Puntos clave:

- **VPS 2 (redis)**: `SUBGEN_REDIS_URL` puede quedar con el default
  (`redis://redis:6379/0`, resuelto dentro del propio contenedor). Si
  hosteas MinIO aca tambien (ver [Almacenamiento](#almacenamiento)),
  setea las credenciales S3.
- **VPS 1 (api) y VPS 3/4 (workers)**: `SUBGEN_REDIS_URL` tiene que
  apuntar a la IP PRIVADA de la VPS 2: `redis://10.0.0.12:6379/0`.
- Las 4 necesitan las mismas `SUBGEN_S3_*` (mismo bucket).
- Solo VPS 1 necesita `SUBGEN_S3_PUBLIC_ENDPOINT_URL` (la URL que va a usar
  el navegador del usuario -- la de Hetzner Object Storage, o la IP/dominio
  publico donde expongas tu MinIO).
- Solo VPS 1 necesita `SUBGEN_CORS_ORIGINS` (el dominio publico de tu
  frontend).

### 5. Orden de arranque

```bash
# VPS 2 primero (todo depende de Redis):
docker compose -f docker-compose.prod.yml up -d redis
# si elegiste MinIO self-hosted en esta misma VPS:
docker compose -f docker-compose.prod.yml --profile selfhosted-storage up -d minio

# VPS 3 y VPS 4 (workers, en cualquier orden entre si):
docker compose -f docker-compose.prod.yml up -d worker-1   # VPS 3, con Celery Beat
docker compose -f docker-compose.prod.yml up -d worker-2   # VPS 4

# VPS 1 al final:
docker compose -f docker-compose.prod.yml up -d api
```

Los clientes de Redis/Celery reintentan la conexion automaticamente, asi
que el orden no es estrictamente obligatorio -- pero arrancar Redis primero
evita ruido de logs de reconexion al principio.

### 6. Verificar

```bash
curl http://<ip-publica-vps1>/api/health
```

Y de ahi, correr el [checklist de verificacion manual](#checklist-de-verificacion-manual)
completo antes de darlo por andando.

### TLS / dominio propio

`docker-compose.prod.yml` expone la API en el puerto 80 sin TLS. Para
produccion real, pone un reverse proxy (Caddy o nginx + certbot) delante
de la VPS 1 y apunta el puerto 443 ahi -- esta fuera del alcance de este
proyecto de portfolio, pero es el paso obvio siguiente.

---

## Correr los tests

Worker y API tienen dependencias muy distintas (el worker instala
faster-whisper/deep-translator/yt-dlp; la API no) y **ambos declaran un
paquete top-level llamado `app`** -- por diseno, para que cada uno se vea a
si mismo como `from app.something import ...` tanto corriendo local como
dentro de su propio contenedor. Eso significa que **no se pueden correr
todos los tests en el mismo entorno virtual**: hay que instalar cada
servicio en su propio venv y correr sus tests por separado, igual que se
buildean en Dockerfiles separados.

```bash
# Worker: logica de pipeline, progreso/ETA, sanitizacion, tareas Celery
python -m venv .venv-worker
.venv-worker/bin/pip install -e "worker[dev]"
cd worker && ../.venv-worker/bin/pytest ../tests/test_subtitles.py ../tests/test_progress.py ../tests/test_pipeline.py --cov=app
cd ..

# API: endpoints, validacion, WebSocket, rate limiting
python -m venv .venv-api
.venv-api/bin/pip install -e "api[dev]"
cd api && ../.venv-api/bin/pytest ../tests/test_api.py --cov=app
cd ..
```

Los tests de la API usan Celery en **modo eager** con un backend en
memoria (`cache+memory://`) y una tarea *stub* registrada bajo el mismo
nombre que usa la API para encolar -- no hace falta Redis real ni el
pipeline pesado del worker. `/api/health` se testea con el cliente de Redis
mockeado.

Type-checking estricto en ambos:

```bash
cd worker && ../.venv-worker/bin/mypy app && cd ..
cd api && ../.venv-api/bin/mypy app && cd ..
```

Estado actual: **75 tests / 79% cobertura** en el worker, **17 tests / 81%
cobertura** en la API, `mypy --strict` limpio en los dos. Todo esto se
corrio de verdad (no es una cifra aspiracional) -- en el camino se
encontraron y corrigieron varios bugs reales: un overflow de milisegundos
en `fmt_ts` (`1.9999s` -> `"01,1000"` en vez de acarrear el segundo), un
`Dockerfile` del worker con rutas de `COPY` que no coincidian con el build
context declarado en el compose, un campo `cors_origins: list[str]` en la
config de la API que rompia porque pydantic-settings intenta decodificar
listas como JSON antes de correr un validador propio, y un `send_task()`
que ignoraba `task_always_eager` en los tests (arreglado usando
`Signature.apply_async()`, que si lo respeta cuando la tarea esta
registrada localmente y cae a `send_task` si no -- mismo comportamiento en
produccion, testeable en eager mode).

---

## Decisiones de arquitectura

### Por que Celery y no threads?

El original usaba `threading.Thread(daemon=True)` por request: no
sobrevive a un restart del proceso, no tiene reintentos, no tiene limite
de tiempo, y sobre todo no es compatible con workers en otra maquina -- el
requisito central de este proyecto (`worker-1`/`worker-2` en VPS
separadas) ya descarta esa opcion por si solo. Celery da cola persistente,
reintentos con backoff, limites de tiempo, y el mismo codigo de tarea
corre igual en un proceso local o en 10 VPS.

### Por que Redis y no RabbitMQ u otro broker?

Alcanza sobradamente para el volumen de este proyecto, es un solo binario
sin configuracion pesada, y sirve para tres cosas a la vez: broker de
Celery, result backend, y canal de Pub/Sub para el progreso en tiempo real
del WebSocket -- un componente menos que correr y operar.

### Almacenamiento

La decision mas importante del proyecto: como comparten archivos el
worker (que los genera) y la API (que los sirve), estando en VPS
distintas.

**Elegido: almacenamiento S3-compatible** (MinIO para desarrollo local,
incluido en `docker-compose.yml`; Hetzner Object Storage, tu propio MinIO,
o AWS S3 en produccion -- se elige solo cambiando `S3_ENDPOINT_URL` y
credenciales). El worker sube el resultado al terminar; la API nunca lee
el filesystem del worker, genera URLs pre-firmadas contra el mismo bucket.

Por que: es el patron estandar para "un proceso genera un archivo, otro lo
sirve" cuando estan en maquinas distintas, no depende de que las 4 VPS
compartan filesystem, escala a mas workers sin tocar infraestructura, y
ademas **resolvio de paso una vulnerabilidad de path traversal** que tenia
el codigo original (`SUBTITLES_DIR / filename` con el filename tomado
directo de la URL, sin sanitizar mas alla de chequear que existiera). En
el diseno nuevo el unico input del usuario en la URL de descarga es
`job_id`, que se usa solo para consultar Celery; el nombre real de archivo
y la key en el bucket salen del resultado que guardo el worker, nunca del
request.

**Alternativa documentada: volumen compartido (NFS/SSHFS)** entre las VPS.
Mas simple en el papel (los workers escriben como si fuera disco local),
pero en produccion distribuida es la parte mas fragil de todo el sistema:
hay que montar NFS por la red privada, manejar permisos, y el servidor NFS
es un punto unico de falla. Si preferis esta ruta, reemplaza
`worker/app/storage.py` y `api/app/storage.py` por operaciones de
filesystem sobre un volumen Docker montado desde el mismo NFS share en las
4 VPS.

### Progreso en tiempo real: Pub/Sub, no polling oculto

En vez de que la API haga polling interno del backend de resultados de
Celery, el worker publica cada actualizacion de progreso a un canal
`progress:{job_id}` en Redis (ver `worker/app/progress.py`) y la API se
suscribe a ese canal por cada WebSocket conectado (`api/app/websocket.py`)
y reenvia en tiempo real. El endpoint REST `GET /api/jobs/{id}` sigue
disponible como fallback si el WebSocket no puede conectar -- usa el mismo
formato de payload, solo que por polling cada 1.5s desde el frontend en
vez de push.

### Concurrencia de los workers

Cada worker carga su propio modelo Whisper en memoria (singleton por
proceso). Con `concurrency=1` por worker (ver `Dockerfile`/compose), un
job a la vez por VPS -- evita tener N copias del modelo en RAM y
sobresuscribir CPU en tareas ya de por si intensivas (Whisper + FFmpeg).
El escalado es horizontal: mas contenedores worker, no mas threads dentro
de uno -- que es justo lo que `worker-1`/`worker-2` demuestran.

### Limpieza automatica

Celery Beat corre embebido en `worker-1` (flag `-B` de Celery), no como un
quinto servicio -- dado que el enunciado fija 4 VPS. `worker-2` corre sin
ese flag para que la tarea periodica de limpieza no se dispare duplicada.

### Reintentos: solo para fallos transitorios de red

`tasks.py` distingue `DeterministicPipelineError` (URL invalida, sin
audio, video que supera la duracion maxima -- reintentar no cambia nada) de
`TransientPipelineError` (timeout, conexion reseteada, servicio
temporalmente caido en descarga o traduccion -- reintentar con backoff SI
tiene sentido). Solo la segunda categoria dispara `autoretry_for` en la
tarea Celery.

### Camino a Kubernetes (si hiciera falta escalar mas)

Este proyecto usa Docker Compose a proposito (asi lo pide el enunciado),
pero esta disenado para no pelear con una migracion a k8s mas adelante:

- Cada servicio ya es una imagen Docker independiente con su propio
  `Dockerfile` -- se traducen 1:1 a Deployments.
- La configuracion ya es 100% por variables de entorno (Pydantic
  Settings) -- se traduce directo a ConfigMaps/Secrets.
- Redis pasaria a ser un servicio gestionado (o un `StatefulSet` con
  volumen persistente) en vez de un contenedor suelto.
- `worker-1`/`worker-2` se convierten en un unico Deployment con
  `replicas: N` y un HorizontalPodAutoscaler -- la limpieza (Celery Beat)
  se moveria a un `CronJob` separado en vez de vivir embebida en una
  replica particular del worker (ahora mismo depende de que "worker-1"
  sea una instancia fija, que en k8s con replicas intercambiables no
  aplica).
- El bucket S3-compatible no cambia nada -- es exactamente el mismo patron
  en k8s que fuera de el.
- Los health checks de Docker (`HEALTHCHECK`) se traducen directo a
  liveness/readiness probes.

---

## Seguridad

- **Path traversal**: cerrado al redisenar las descargas alrededor de
  `job_id` + Celery + presigned URLs (ver [Almacenamiento](#almacenamiento)).
  El original tenia esta vulnerabilidad activa (`filename` de la URL sin
  sanitizar antes de armar la ruta de disco).
- **Subprocess**: todas las llamadas (yt-dlp interno, ffmpeg, ffprobe) usan
  listas de argumentos, nunca concatenacion de strings para shell.
- **Sanitizacion de nombre de archivo**: `sanitize_filename()` solo permite
  `[a-zA-Z0-9 _-]`, bloquea nombres vacios/reservados de Windows, y trunca
  longitud.
- **Rate limiting**: `POST /api/jobs` limitado por IP (`slowapi`),
  configurable via `SUBGEN_RATE_LIMIT_CREATE_JOB`.
- **CORS**: configurable por entorno, nunca `*` por default.
- **Errores sin fugas**: el handler global de excepciones de FastAPI nunca
  devuelve stack traces ni rutas internas al cliente.
- **Usuario no-root**: los dos contenedores corren como usuario `subgen`
  (uid 1000), no root.
- **Limite de duracion**: video que supera `MAX_VIDEO_DURATION_SECONDS` se
  rechaza antes de gastar computo en transcribirlo.

### Nota operativa: visibility_timeout de Redis (recuperación tras un crash)

Verificado en producción: si un worker muere a mitad de una tarea (crash,
`docker compose down` sin querer, restart del host), Celery con
`acks_late=True` debería devolver esa tarea a la cola para que otro worker
la retome. Pero Redis como broker tiene su propio `visibility_timeout`
(default de kombu: 3600s) — hasta que no pase ese tiempo, considera que el
mensaje "probablemente sigue siendo procesado" y no lo redistribuye. Sin
configurarlo explícitamente, un crash real puede dejar un job mostrando su
último progreso conocido durante **hasta una hora**, sin que nadie lo esté
procesando de verdad.

Está configurado en `celery_app.py` como `task_time_limit + 300` (justo
por encima del tiempo máximo que puede durar una tarea legítima, para no
redistribuir un job que todavía está corriendo bien). Si cambiás
`SUBGEN_CELERY_TASK_TIME_LIMIT`, este valor se ajusta solo.

### Nota operativa: primer job lento (carga del modelo Whisper)

Verificado en vivo: el primer job que procesa CADA proceso worker paga dos
costos que no se repiten después:

1. **Descarga del modelo** desde HuggingFace (cientos de MB) la primera vez
   que ESE CONTENEDOR lo necesita — persiste en el volumen nombrado
   `whisper-cache` (`/home/subgen/.cache/huggingface` dentro del
   contenedor), así que sobrevive a restarts y rebuilds de la imagen. Sin
   este volumen, cada `docker compose build --no-cache` fuerza una descarga
   nueva completa.
2. **Carga del modelo en memoria** (singleton por proceso, ver
   `worker/app/pipeline/transcribe.py`) — esto se repite en cada restart
   del contenedor (no hay forma de evitarlo, es cargar pesos a RAM), pero
   es mucho más rápido que la descarga: unos segundos, no decenas.

Con dos workers (`worker-1`/`worker-2`), cada uno tiene su PROPIO proceso y
su propio modelo cargado en memoria — Celery reparte los jobs al que esté
libre, así que no hay control directo sobre cuál toma cada uno. Es
esperable que los primeros dos jobs (uno por worker) sean más lentos que
los siguientes.

### Nota operativa: caida prolongada de Redis

Verificado en vivo (levantando la API real con Redis apagado a proposito):
si Redis deja de responder, Celery agota sus propios reintentos de
reconexion y el pool de conexiones del backend queda en un estado que su
propio log describe como "the Celery application must be restarted". La
API no se cae -- cada `POST /api/jobs` devuelve `503` de forma limpia (ver
`QueueUnavailableError` en `api/app/celery_client.py`) -- pero, dependiendo
de cuanto haya durado el corte, el proceso de la API puede necesitar un
restart manual (`docker compose restart api`) incluso despues de que Redis
vuelva, porque ese pool interno de Celery no siempre se recupera solo. En
`docker-compose.yml`/`.prod.yml` el `healthcheck` de `api` va a marcar el
contenedor como `unhealthy` en ese escenario, pero Docker Compose (a
diferencia de Swarm/k8s) no reinicia automaticamente por un healthcheck
fallido -- si automatizas el redeploy, agrega un watchdog externo
(`autoheal`, un cron simple, o el liveness probe correspondiente si migras
a k8s) que reinicie contenedores `unhealthy`.

---

## Checklist de verificacion manual

Antes de considerar esto "andando" en un entorno nuevo (local o VPS):

1. **Servicios arriba**: `docker compose ps` -- los 5 contenedores (o 4 en
   prod sin MinIO) en estado `healthy`, no solo `running`.
2. **Health check**: `curl http://localhost:8000/api/health` -> `{"status":
   "ok", "redis": true}`.
3. **Frontend carga**: abrir `http://localhost:8000` -- se ve el logo, el
   tema oscuro por default (o el que tenga el sistema operativo), sin
   flash blanco al cargar.
4. **Tema claro**: click en el boton de tema -- cambia sin recargar,
   persiste al refrescar la pagina (`localStorage`).
5. **Idioma de interfaz**: click en el boton "EN" -- todos los textos de la
   UI cambian (encabezados, botones, footer). El `<select>` de "Traducir
   a:" NO cambia de idioma con este boton -- son cosas distintas, y hay un
   texto aclarandolo debajo del select.
6. **Job modo SRT**: pegar una URL de YouTube corta (~1-2 min), modo "Solo
   subtitulos" -- si da `"Sign in to confirm you're not a bot"`, ver
   [Cookies de YouTube](#cookies-de-youtube), o probar primero con
   `https://archive.org/details/big-buck-bunny-720-10s-5-mb-1` (dominio
   publico, sin bloqueo) para validar que el pipeline en si funciona.
   Verificar:
   - La barra de progreso avanza de forma continua (no saltos fijos).
   - Los mensajes de progreso van cambiando (descarga -> transcripcion ->
     traduccion).
   - Al terminar, aparece el boton de descarga y la vista previa de los
     primeros segmentos.
   - El archivo `.srt` descargado abre bien y tiene los timestamps
     correctos.
7. **Job modo video**: mismo video, modo "Video con subtitulos". Verificar
   ademas:
   - Durante el quemado, el mensaje muestra porcentaje Y un ETA
     (`"Quemando subtitulos -- 47% . faltan ~2m 15s"` o el equivalente en
     ingles).
   - El `.mp4` descargado tiene los subtitulos quemados con el estilo
     esperado (Arial, blanco con borde negro).
8. **WebSocket real**: con las devtools abiertas (pestana Network -> WS),
   confirmar que hay una conexion WebSocket activa durante el
   procesamiento, no requests HTTP repetidos cada 1.5s.
9. **Fallback a polling**: cortar el WebSocket a mano (bloquear el puerto,
   o simular con devtools) y confirmar que el progreso sigue actualizando
   via polling REST.
10. **URL invalida**: probar con `no-es-una-url` -> error claro, sin
    stack trace, sin tumbar el job anterior.
11. **Rate limit**: mandar 6+ requests seguidos a `POST /api/jobs` -> el
    6to (segun el limite configurado) devuelve 429.
11b. **Redis caido**: parar el contenedor `redis` con un job en curso y
    confirmar que `POST /api/jobs` responde `503` (no `500`) mientras dure
    el corte, y que al levantar `redis` de nuevo la API vuelve a aceptar
    jobs sin intervencion manual — si no, ver la nota operativa de la
    seccion Seguridad y reiniciar el contenedor `api`.
12. **Escalado horizontal**: con un job en curso, matar `worker-1` a mitad
    de proceso (`docker compose kill worker-1`) -- gracias a
    `acks_late=True`, la tarea tiene que reaparecer y la termina
    `worker-2` (puede reiniciar desde el principio, no hay checkpointing
    intra-tarea, pero no se pierde silenciosamente).
13. **Limpieza automatica**: confirmar en los logs de `worker-1` que la
    tarea `cleanup_expired_outputs` corre periodicamente (cada
    `SUBGEN_CLEANUP_INTERVAL_SECONDS`).
14. **Logs estructurados**: confirmar que los logs de `api` y de los
    workers son JSON, y que un mismo `job_id` aparece correlacionado en
    los logs de ambos servicios para un mismo trabajo.
