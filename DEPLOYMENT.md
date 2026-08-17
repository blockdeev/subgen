# Despliegue en Hetzner — guía y razonamiento

Este documento reemplaza la guía de despliegue que antes vivía en el
README. Cambio de fondo respecto a la primera versión: el reparto de VPS
no es "1 servicio = 1 VPS" -- se agrupó por PERFIL DE CARGA real, no por
tipo de servicio.

## Por qué se rebalanceó

El diseño original (4 VPS, una por servicio: api, redis, worker-1,
worker-2) parece prolijo pero reparte mal el presupuesto real: `api` y
`redis` están casi inactivos el 99% del tiempo (la API solo despacha
requests HTTP livianos y consulta estado; Redis es puro key-value en
memoria) mientras el worker sostiene CPU al 100% durante decenas de
minutos por job. Pagar una VPS entera por un servicio que apenas la usa,
mientras el cuello de botella real (transcripción + quemado) se queda con
la misma capacidad que si hubiera una sola VPS de worker, es presupuesto
mal puesto.

**Reparto nuevo:**

| Grupo | Servicios | Perfil de carga |
|---|---|---|
| 1 VPS chica | `api` + `redis` (+ `minio` si autohosteás storage) | I/O y red, casi nunca CPU-bound |
| N VPS de worker | `worker-1`, `worker-2`, `worker-3`, ... | CPU al 100% sostenido, minutos a decenas de minutos por job |

Con el mismo presupuesto de 4 VPS que el diseño original, esto se traduce
en **1 VPS combinada + 3 VPS de worker** en vez de 1+1+2 -- un 50% más de
capacidad de cómputo real (que es donde efectivamente se hace el trabajo)
sin gastar un centavo más.

## Tres caminos para los workers -- CAX (ARM), CPX, o CCX

Hetzner ofrece tres líneas de instancia relevantes para el worker (CPU-bound,
sostenido, minutos u horas por job):

- **CAX** (ARM64, Ampere Altra, vCPU compartido): la línea más nueva del
  catálogo. Esta carga (FFmpeg + CTranslate2) corre nativa en ARM64 --
  verificado a nivel de dependencias, ver más abajo.
- **CPX** (x86_64 AMD, vCPU compartido): la línea "de siempre".
- **CCX** (x86_64 AMD, vCPU dedicado): sin steal time, pero al precio
  actual cuesta ~2x lo que CPX a cores equivalentes (ver razonamiento
  completo más abajo).

### Pricing -- con una advertencia que hay que tomar en serio

**Hetzner ajustó precios cuatro veces durante 2026** (al menos dos rondas
grandes: 1 de abril y 15 de junio, con impacto MUY distinto por línea de
producto -- CCX y CPX subieron 113-204% en la ronda de junio, mientras que
CAX y CX subieron solo ~30-33% la misma ronda). Encontré fuentes con
fechas de captura distintas que se contradicen entre sí en los números
exactos de CAX -- **la tabla de abajo es una referencia para razonar el
criterio de decisión, no un precio para presupuestar sin confirmar**.
Verificá siempre en [hetzner.com/cloud](https://www.hetzner.com/cloud/)
al momento de contratar.

| Instancia | Arquitectura | vCPU | RAM | Precio/mes (EU, estimado ago-2026) |
|---|---|---|---|---|
| CAX21 | ARM64 compartido | 4 | 8 GB | ~€10 |
| CAX31 | ARM64 compartido | 8 | 16 GB | ~€21 |
| CAX41 | ARM64 compartido | 16 | 32 GB | ~€41 |
| CPX41 | x86_64 compartido | 8 | 16 GB | ~€69 |
| CCX33 | x86_64 dedicado | 8 | 32 GB | €138.49 (confirmado) |

Si estos números aproximados de CAX/CPX se sostienen, un **CAX31 (8 vCPU
ARM) cuesta aproximadamente lo mismo que la MITAD de cores en CPX** -- a
precio comparable, el doble de cores. Para una carga puramente CPU-bound
como esta (sin nada que dependa de instrucciones específicas de x86), eso
es exactamente la métrica que importa. Pero esto es una proyección de
precio, no de rendimiento real -- Ampere Altra y AMD EPYC no rinden igual
core-por-core en esta carga específica, por eso hace falta medir (ver
"Plan de medición en un CAX real" más abajo) antes de comprometerse.

### ¿Corre esta carga en ARM64 de verdad? Verificado, no asumido

Antes de recomendar nada, resolví el árbol de dependencias completo del
worker y de la API contra wheels de `manylinux aarch64` (sin poder
compilar nada desde source):

| Paquete | Estado en ARM64 |
|---|---|
| `ctranslate2` (motor real detrás de faster-whisper) | ✅ wheel oficial (`manylinux_2_28_aarch64`) |
| `faster-whisper` | ✅ puro Python, sin problema |
| `av` (PyAV, decodifica el audio) | ✅ wheel oficial desde la 15.0.0 (`faster-whisper` no fija tope de versión, resuelve bien) |
| `onnxruntime` (VAD de faster-whisper) | ✅ wheel oficial |
| `tokenizers` | ✅ wheel oficial (tag `manylinux_2_17_aarch64`, compatible) |
| `numpy`, `protobuf`, `pydantic-core` | ✅ wheels oficiales |
| Resto del árbol (celery, redis, boto3, yt-dlp, fastapi, uvicorn, etc.) | ✅ todo resuelve, la mayoría son paquetes puros de Python |
| `ffmpeg` (paquete de Debian, no de pip) | ✅ Debian soporta arm64 como arquitectura de release completa |

**Nada necesita compilarse desde source.** Los Dockerfiles actuales
(`worker/Dockerfile`, `api/Dockerfile`) no tienen nada hardcodeado a
x86_64 -- la imagen base `python:3.11-slim` es multi-arch nativa, así que
construyen para ARM64 sin ningún cambio.

### Buildear para ARM64

**Para el test rápido en un CAX de prueba (recomendado primero)**: no
hace falta cross-compilar nada. Arrancás el CAX, instalás Docker ahí, y
hacés `docker compose build` **nativo** -- mucho más simple y rápido que
cross-compilar con QEMU:

```bash
# DENTRO del CAX de prueba (ya es ARM64, build nativo):
curl -fsSL https://get.docker.com | sh
git clone <tu-repo> subgen && cd subgen
docker compose build worker-1
```

**Para publicar una imagen multi-arch más adelante** (si decidís que ARM
queda como parte permanente del despliegue, y querés una sola imagen que
sirva tanto para tus VPS x86 como ARM sin mantener dos builds separados),
`docker buildx` sí permite cross-compilar y publicar ambas arquitecturas
desde una sola máquina:

```bash
# Una sola vez, para preparar el builder multi-plataforma:
docker buildx create --name subgen-builder --use
docker buildx inspect --bootstrap

# Build + push multi-arch (necesita un registry, ej. Docker Hub o GHCR):
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f worker/Dockerfile \
  -t tu-usuario/subgen-worker:latest \
  --push .

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f api/Dockerfile \
  -t tu-usuario/subgen-api:latest \
  --push .
```

El cross-build de amd64→arm64 vía QEMU es notablemente más lento que un
build nativo (la emulación de instrucciones tiene costo real, sobre todo
compilando dependencias con partes en C/Rust) -- para el test puntual en
un CAX de prueba, el build nativo de arriba es la opción práctica.

### Plan de medición en un CAX real (por horas, centavos)

1. Creá un **CAX31** (8 vCPU, buen punto medio para medir sin gastar de
   más) desde el panel de Hetzner. No hace falta integrarlo a la red
   privada ni al resto del despliegue -- este test es standalone.
2. Instalá Docker, cloná el repo, y levantá el `docker-compose.yml`
   completo (no el `.prod.yml`) para tener un entorno autocontenido en esa
   misma VPS -- redis, minio, api, y worker, todo local a esa máquina, sin
   depender de nada externo:
   ```bash
   docker compose build
   docker compose up -d
   sleep 20
   docker compose ps   # confirmar que los 5 salen healthy
   ```
3. Mandá el **mismo video de referencia** que venimos usando en todo este
   proyecto (`https://www.youtube.com/watch?v=m-zhU5tq4dc`, 34:16), modo
   video, 30fps (el default ya configurado).
4. Capturá el desglose exacto como veníamos haciendo:
   ```bash
   docker compose logs worker-1 worker-2 | grep -E "received|succeeded|etapa"
   ```
5. Comparalo directo contra la tabla que ya tenés de la máquina de
   desarrollo (burn=348s a `veryfast/crf23`/30fps, transcribe=~260s,
   translate=~30s) -- no son directamente comparables núcleo a núcleo
   (32 cores x86 vs 8 cores ARM), pero te da una primera señal de si el
   rendimiento por-core de Ampere Altra en esta carga específica es
   competitivo o no.
6. **Apagá el CAX en cuanto termines de medir** -- por eso conviene un
   test standalone de pocas horas, no dejarlo corriendo integrado al resto
   del despliegue hasta confirmar que vale la pena.

Con ese dato real (no una extrapolación), recién ahí se decide si CAX
reemplaza a CPX/CCX como línea principal para los workers de producción.

### CCX vs. CPX -- el argumento de steal time, y por qué cuesta más caro sostenerlo hoy

Entre las dos líneas x86 (CPX compartido, CCX dedicado), la diferencia
técnica es real: en un vCPU compartido, si otro cliente en el mismo host
físico también está exigiendo CPU al mismo tiempo, el hypervisor le
"roba" ciclos a tu VM para dárselos al otro inquilino -- esto se mide
como **steal time**, y es exactamente el escenario donde más duele: cargas
sostenidas y prolongadas como esta, no ráfagas cortas. Con CCX no hay ese
riesgo, los cores son tuyos todo el tiempo.

Pero como muestra la tabla de arriba, a cores equivalentes CCX cuesta
aproximadamente **el doble** que CPX con los precios post-junio-2026 --
antes de ese ajuste la brecha era más cercana a 30-50%. Pagar 2x para
eliminar un riesgo que depende de cuán cargado esté el host físico que te
toque (algo que no controlás ni podés saber de antemano) es una apuesta
más cara de lo que era hace unos meses.

**Recomendación, coherente con cómo venimos decidiendo todo en este
proyecto (medir antes de comprometerse):**

1. Arrancá con **CAX o CPX** (lo que haya dado mejor en tu test de
   medición) -- nunca CCX de entrada.
2. Medí el tiempo real de un job completo en producción (sección de abajo)
   y compará contra la proyección de tu benchmark local.
3. Si coincide (sin degradación inexplicada), quedate como está -- el
   ahorro es real y el riesgo no se materializó.
4. Si ves tiempos inconsistentes entre corridas del mismo video, o
   sistemáticamente peores que lo proyectado -- señal concreta de steal
   time, y ahí sí migrás esos workers puntuales a CCX, con el mismo
   diagnóstico que ya usamos en este proyecto (`docker exec ... env`,
   logs de `etapa`, `FFmpeg: quemando subs`) para confirmarlo con datos.

Para `api`/`redis`/`minio` (la VPS chica), CPX sigue siendo la elección
clara sin ninguna duda -- ahí el patrón de carga (ráfagas cortas de I/O,
no cómputo sostenido) es exactamente el que un vCPU compartido maneja
bien.

## Cómo desplegar

### 1. Elegí el tamaño de cada VPS

- **VPS combinada** (api+redis+minio): CPX21 o similar alcanza -- esta
  VPS no hace cómputo pesado, no tiene sentido gastar en ARM o dedicado
  para esto tampoco.
- **VPS de worker**: si ya mediste en un CAX de prueba (sección de
  arriba) y el rendimiento por-core resultó competitivo, **CAX es la
  opción con mejor relación costo/cores hoy**. Si no lo mediste todavía
  o el resultado no fue bueno, **CPX31 o CPX41** como segunda opción --
  y medí en producción real (ver más abajo) antes de pagar el 2x de CCX.

### 2. Red privada

Igual que antes: **Networks → Create Network** en el panel de Hetzner
Cloud, agregá todas las VPS (la combinada y todas las de worker) a esa
red. Anotá las IPs privadas de cada una.

### 3. Instalar Docker en todas

```bash
curl -fsSL https://get.docker.com | sh
```

### 4. Copiar el repo a todas

```bash
git clone <tu-repo> subgen && cd subgen
```

### 5. Configurar el `.env` de cada VPS

- **VPS combinada**: `SUBGEN_REDIS_URL=redis://redis:6379/0` (resuelve
  por nombre de servicio dentro de la misma VPS, no hace falta IP).
  `SUBGEN_S3_PUBLIC_ENDPOINT_URL` con la URL que va a usar el navegador
  del usuario. `SUBGEN_CORS_ORIGINS` con tu dominio público.
- **VPS de worker**: `SUBGEN_REDIS_URL=redis://<IP-PRIVADA-DE-LA-VPS-COMBINADA>:6379/0`.
  Mismas `SUBGEN_S3_*` que la combinada (mismo bucket).

### 6. Orden de arranque

```bash
# VPS combinada primero:
docker compose -f docker-compose.prod.yml up -d api redis
# + minio si autohosteás storage:
docker compose -f docker-compose.prod.yml --profile selfhosted-storage up -d minio

# Cada VPS de worker (en cualquier orden entre sí):
docker compose -f docker-compose.prod.yml up -d worker-1   # con Celery Beat
docker compose -f docker-compose.prod.yml up -d worker-2
docker compose -f docker-compose.prod.yml up -d worker-3
# agregá worker-N calcando el bloque de worker-2 en docker-compose.prod.yml
# si tu presupuesto da para más VPS de worker
```

### 7. Verificar

```bash
curl http://<ip-pública-vps-combinada>/api/health
```

Y de ahí, el [checklist de verificación manual](README.md#checklist-de-verificación-manual)
completo del README.

## Medir en producción, apenas esté arriba

Todas las decisiones de rendimiento de este proyecto (códec, fps de
salida, preset/crf, traducción concurrente, y ahora CCX vs CPX) están
basadas en mediciones sobre **una máquina de desarrollo de 32 cores**, no
en el hardware real de producción. El burn distribuido quedó
explícitamente pendiente de este dato (ver `ARCHITECTURE.md`) -- pero en
rigor, **todas** las proyecciones de esta sección de deploy también.

Apenas tengas la VPS de worker arriba, corré un video real de ~1 hora
(no el de 34 minutos que usamos en desarrollo -- más largo, para que
cualquier variación se note más) y capturá el desglose por etapa:

```bash
docker compose logs worker-1 worker-2 | grep -E "received|succeeded|etapa"
```

Comparalo contra lo que predeciría tu curva de threads local, ajustada al
core count real de la VPS que elegiste (la tabla de la sección anterior
sobre CCX/CPX ya te da el punto de partida). Si el tiempo real:

- **Coincide** con la proyección (dentro de ~10-15% de margen, que es el
  orden de la varianza que ya vimos entre corridas "idénticas" en
  desarrollo) -- confirmado, seguí con la config actual.
- **Es sistemáticamente peor**, sobre todo si varía mucho entre corridas
  del mismo video -- señal de steal time si estás en CPX (migrá esos
  workers a CCX), o de otro cuello de botella específico del hardware que
  hay que investigar con el mismo método que usamos acá (logs de `etapa`,
  `docker exec ... env`, comparar contra el benchmark aislado).

Con ese número real (no proyectado) es que corresponde decidir si el burn
distribuido (`ARCHITECTURE.md`) sigue valiendo la complejidad.

## TLS / dominio propio

`docker-compose.prod.yml` expone la API en el puerto 80 sin TLS. Para
producción real, poné un reverse proxy (Caddy o nginx + certbot) delante
de la VPS combinada y apuntá el puerto 443 ahí.

## Alternativa: si preferís el reparto original (1 servicio = 1 VPS)

Sigue siendo válido si por alguna razón preferís aislar cada servicio en
su propia VPS (por ejemplo, políticas de seguridad que exigen separación
física) -- simplemente corré `up -d api`, `up -d redis`, `up -d worker-N`
cada uno en su propia VPS en vez de agrupar `api redis` juntos. El
`docker-compose.prod.yml` funciona igual de cualquiera de las dos formas,
esto es una decisión de despliegue, no una restricción del archivo.
