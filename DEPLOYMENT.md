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

## Cómo desplegar -- checklist ejecutable de punta a punta

Asume que arrancás de cero, con las VPS recién creadas y nada más.

### 1. Elegí el tamaño de cada VPS

- **VPS combinada** (api+redis+minio): CPX21 o similar alcanza -- esta
  VPS no hace cómputo pesado, no tiene sentido gastar en ARM o dedicado
  para esto tampoco.
- **VPS de worker**: si ya mediste en un CAX de prueba (sección de
  arriba) y el rendimiento por-core resultó competitivo, **CAX es la
  opción con mejor relación costo/cores hoy**. Si no lo mediste todavía
  o el resultado no fue bueno, **CPX31 o CPX41** como segunda opción --
  y medí en producción real (ver más abajo) antes de pagar el 2x de CCX.
  Con 1-2 workers de presupuesto, priorizá **cores sobre cantidad de
  VPS**: un solo worker con más cores procesa un job individual más
  rápido; dos workers más chicos dan más throughput (jobs concurrentes)
  pero cada uno sigue siendo tan lento como sus propios cores. Si el uso
  real es "un usuario a la vez, quiero que sea rápido", priorizá cores
  en un solo worker. Si es "varios usuarios en simultáneo", priorizá
  cantidad.

### 2. Red privada

**Networks → Create Network** en el panel de Hetzner Cloud, agregá todas
las VPS (la combinada y todas las de worker) a esa red. Anotá las IPs
privadas de cada una.

### 3. Actualizar el sistema e instalar Docker en todas

```bash
apt update
apt upgrade -y
```

Si actualiza el kernel, te va a pedir reiniciar (`reboot`) -- es seguro, todos los
contenedores de este proyecto usan `restart: unless-stopped` y Docker
arranca solo al boot, así que todo vuelve solo después del reinicio.
Esperá un minuto y reconectate por SSH.

```bash
curl -fsSL https://get.docker.com | sh
```

### 4. Firewall -- ANTES de levantar ningún contenedor

Regla de oro de este proyecto: **Redis y MinIO nunca expuestos a
internet, solo a la red privada.** Sin esto, cualquiera con la IP
pública de tu VPS combinada puede leer/escribir tu Redis (sin
contraseña, ver nota de Secrets abajo) o listar tu bucket de S3.

En **cada VPS** (`ufw` viene preinstalado en las imágenes estándar de
Ubuntu de Hetzner; ajustá si usás otra distro):

```bash
# Default: negar todo entrante, permitir todo saliente
ufw default deny incoming
ufw default allow outgoing

# SSH -- siempre, o te quedás afuera de la VPS
ufw allow 22/tcp
```

**En la VPS combinada, además:**

```bash
# API: pública (80/443, el frontend y las requests de los usuarios)
ufw allow 80/tcp
ufw allow 443/tcp

# Redis: SOLO desde la red privada de Hetzner, nunca 0.0.0.0
ufw allow from 10.0.0.0/8 to any port 6379 proto tcp

# MinIO S3 API (9000): necesita ALCANCE PÚBLICO igual -- el navegador del
# usuario baja el archivo final directo desde ahí vía URL pre-firmada, no
# a través de la API. Si te incomoda exponerlo, la alternativa es usar
# Hetzner Object Storage / S3 real en vez de autohostear MinIO (evita
# este problema del todo, ver .env.example).
ufw allow 9000/tcp

# Consola de administración de MinIO (9001): NUNCA pública, ni siquiera
# hace falta en producción salvo que quieras administrarla a mano.
ufw allow from 10.0.0.0/8 to any port 9001 proto tcp
```

**En cada VPS de worker**: no hace falta abrir NADA más allá de SSH --
los workers solo hacen conexiones salientes (a Redis, a MinIO, a
YouTube, a Google Translate), nunca reciben conexiones entrantes de
nadie.

Activar al final, en cada VPS:

```bash
ufw enable
ufw status verbose   # confirmar que quedó como esperabas ANTES de seguir
```

(ajustá `10.0.0.0/8` al rango real de tu red privada de Hetzner -- confirmalo
en el panel, Networks → tu red → no asumas el rango)

### 5. Copiar el repo a todas

```bash
git clone <tu-repo> subgen && cd subgen
```

### 6. Secrets -- nunca los defaults del repo en producción

`.env.example` trae credenciales de juguete (`SUBGEN_S3_ACCESS_KEY=subgen`,
`SUBGEN_S3_SECRET_KEY=subgen12345`, mismo para `MINIO_ROOT_USER`/
`MINIO_ROOT_PASSWORD`) -- están ahí para que el entorno local funcione
sin fricción, **nunca para producción**. Generá credenciales reales:

```bash
# access key y secret key para MinIO/S3 -- generá algo random, no un
# password memorable
openssl rand -hex 16   # para SUBGEN_S3_ACCESS_KEY
openssl rand -hex 32   # para SUBGEN_S3_SECRET_KEY (y MINIO_ROOT_PASSWORD, el mismo valor)
```

Redis no tiene contraseña configurada -- su seguridad depende
**enteramente** de que el firewall del paso 4 esté bien puesto (nunca
expuesto a `0.0.0.0`). Si querés una capa extra de defensa, se le puede
agregar `requirepass` a la imagen de Redis, pero no es parte del setup
por default de este proyecto -- si te importa, avisame y lo agregamos
antes de desplegar, no después.

### 7. Cookies de YouTube -- probable primer bloqueante real

Desde una IP de datacenter, YouTube bloquea sesiones sin cookies mucho
más agresivo que desde una IP residencial. Es muy probable que el primer
job real desde el VPS falle sin esto -- hacelo ANTES de mandar nada.

**Exportar** (elegí una, con una sesión de YouTube logueada):

- Extensión de navegador "Get cookies.txt LOCALLY" (Chrome/Firefox): entrá
  a youtube.com logueado, exportá en formato Netscape.
- O con `yt-dlp` instalado en tu máquina local (no en el contenedor):
  ```bash
  yt-dlp --cookies-from-browser chrome --cookies cookies.txt --skip-download "https://youtube.com/watch?v=dQw4w9WgXcQ"
  ```

**Subir al VPS de worker** (cada VPS de worker necesita su propia copia):

```bash
scp cookies.txt usuario@ip-del-worker:/home/usuario/subgen/cookies.txt
```

**Configurar** en el `.env` de cada VPS de worker:

```
YTDLP_COOKIES_FILE_HOST=/home/usuario/subgen/cookies.txt
```

**Verificar ANTES de mandar el primer job real** -- dos chequeos, uno
rápido (el archivo tiene contenido válido) y uno real (yt-dlp lo usa de
verdad contra YouTube):

```bash
# 1. El archivo tiene cookies de youtube.com de verdad (no vacío, no corrupto)
docker exec subgen-worker-1-1 python3 -c "
import http.cookiejar
jar = http.cookiejar.MozillaCookieJar('/worker/cookies.txt')
jar.load(ignore_discard=True, ignore_expires=True)
yt = [c for c in jar if 'youtube.com' in c.domain]
print(f'{len(yt)} cookies de youtube.com cargadas')
assert len(yt) > 0, 'el archivo no tiene cookies de youtube.com -- revisar la exportación'
"

# 2. Prueba real contra YouTube, sin descargar nada (rápido)
docker exec subgen-worker-1-1 yt-dlp --cookies /worker/cookies.txt \
  --skip-download --simulate "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

Si el paso 2 imprime el título del video y metadata sin errores, las
cookies funcionan. Repetí en `worker-2`/`worker-3` (cada worker tiene su
propio archivo, verificalos todos).

**Cómo reconocer cookies vencidas más adelante**: el error que vas a ver
(en el detalle del job fallido, o en `docker compose logs worker-N`) va a
mencionar **"sign in"** en el mensaje y, si viene de yt-dlp con cookies ya
configuradas, suele incluir la frase **"exporting YouTube cookies"** en el
hint que agrega la propia librería (el texto exacto de YouTube varía, esa
parte de yt-dlp es estable). Si ves eso, repetí la exportación -- las
cookies de sesión no duran para siempre.

```bash
docker compose logs worker-1 worker-2 | grep -iE "sign in|exporting.*cookies"
```

### 8. Configurar el resto del `.env` de cada VPS

- **VPS combinada**: `SUBGEN_REDIS_URL=redis://redis:6379/0` (resuelve
  por nombre de servicio dentro de la misma VPS, no hace falta IP).
  `SUBGEN_S3_PUBLIC_ENDPOINT_URL` con la URL que va a usar el navegador
  del usuario. `SUBGEN_CORS_ORIGINS` con tu dominio público. Las
  credenciales del paso 6, no las de `.env.example`.

  **`PRIVATE_IP` -- no te lo saltees, es crítico.** `docker-compose.prod.yml`
  bindea Redis y la consola de MinIO con `${PRIVATE_IP:-0.0.0.0}`: si esta
  variable no está seteada, el fallback es `0.0.0.0` -- público, sin
  autenticación en Redis. **El firewall de `ufw` del paso 4 NO te salva
  acá**: Docker manipula `iptables` directamente al publicar puertos, y
  esas reglas pueden saltearse las de `ufw` (problema real y documentado,
  no una posibilidad remota -- lo confirmamos en vivo durante este
  despliegue). Conseguí la IP privada real de esta VPS en el panel de
  Hetzner (Servers → tu servidor → se ve junto a la IP pública) y agregala:
  ```
  PRIVATE_IP=10.0.0.X
  ```
  Después de setearla (o cambiarla), siempre recreá los contenedores
  afectados para que el binding nuevo tome efecto:
  ```bash
  docker compose -f docker-compose.prod.yml up -d redis minio
  ```
  **Verificá de verdad, no asumas** -- desde tu máquina local, no desde la
  VPS:
  ```bash
  timeout 3 bash -c "echo > /dev/tcp/<IP-PÚBLICA-DE-LA-VPS>/6379" \
    && echo "PUERTO ABIERTO -- mal" || echo "cerrado o timeout -- bien"
  ```
  Tiene que decir "cerrado o timeout". Confirmá también con
  `docker compose -f docker-compose.prod.yml ps` que la columna `PORTS`
  muestre la IP privada (`10.0.0.X:6379->6379/tcp`) y nunca `0.0.0.0` para
  Redis ni para el puerto 9001 de MinIO.

- **VPS de worker**: `SUBGEN_REDIS_URL=redis://<IP-PRIVADA-DE-LA-VPS-COMBINADA>:6379/0`.
  Mismas `SUBGEN_S3_*` que la combinada (mismo bucket). Cookies del paso 7.
  Los workers no publican ningún puerto propio, así que `PRIVATE_IP` no
  aplica de este lado -- solo importa en la VPS combinada.

### 9. TLS con Caddy -- para el dominio público

Caddy hace TLS automático (Let's Encrypt) sin configuración manual de
certificados. En la VPS combinada, corré Caddy delante de la API en vez
de exponer el puerto 8000/80 directo:

```bash
# Instalar Caddy (Ubuntu/Debian):
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

`/etc/caddy/Caddyfile`:

```
tu-dominio.com {
    reverse_proxy localhost:8000
}
```

Reiniciar Caddy (`sudo systemctl restart caddy`) y listo -- HTTPS
automático, certificado renovado solo. Con esto activo, en
`docker-compose.prod.yml` el puerto de `api` puede quedar bindeado solo a
`127.0.0.1:8000:8000` en vez de `80:8000` público directo (Caddy es el
único que necesita alcanzarlo, y ya corre en la misma máquina).

Actualizá `ufw` para reflejar el cambio: cerrá el 80/8000 directo, dejá
solo 443 (y 80 si querés que Caddy redirija http→https automático, que
es su comportamiento por default):

```bash
ufw delete allow 80/tcp    # si lo habías abierto en el paso 4
ufw allow 80/tcp           # Caddy lo usa para el challenge de Let's Encrypt y el redirect
ufw allow 443/tcp
```

### 10. Orden de arranque

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

### 11. Verificación post-deploy -- confirmar que todo se habla ANTES del primer job real

No asumas que porque los contenedores están "Up" están bien conectados
entre sí -- confirmá cada pieza:

```bash
# 1. Los 5 healthy, no solo "running"
docker compose -f docker-compose.prod.yml ps

# 2. La API responde (desde afuera, con TLS si ya configuraste Caddy)
curl https://tu-dominio.com/api/health
# esperá algo como {"status": "ok", ...} -- si dice "degraded", Redis no
# está alcanzable desde la API, revisar el paso 4 (firewall) o la IP
# privada del paso 8

# 3. Cada worker puede hablar con Redis (el broker de Celery)
docker exec subgen-worker-1-1 celery -A app.celery_app inspect ping
# tiene que responder "pong" -- si se cuelga o da timeout, Redis no es
# alcanzable desde ESE worker puntual (firewall o IP privada mal puesta)

# 4. Cookies de YouTube (paso 7, si todavía no lo hiciste)

# 5. Redis y la consola de MinIO NO son alcanzables desde internet --
#    no es opcional, confirmalo siempre, desde tu máquina local (no
#    desde la VPS):
timeout 3 bash -c "echo > /dev/tcp/<IP-PÚBLICA-DE-LA-VPS-COMBINADA>/6379" \
  && echo "PUERTO ABIERTO -- mal, revisar PRIVATE_IP en .env (paso 8)" \
  || echo "cerrado o timeout -- bien"

# 6. Recién ahí: un job real de prueba, corto, para confirmar el pipeline
#    completo de punta a punta antes de anunciar nada públicamente
```

### 12. Qué mirar en los logs las primeras horas

```bash
docker compose -f docker-compose.prod.yml logs -f worker-1 worker-2 api
```

Prestá atención puntual a:

- **`etapa 'download'` con tiempos mucho más largos que en tu máquina de
  desarrollo** -- puede ser ancho de banda del datacenter (raro, suelen
  tener mejor conectividad que una casa) o throttling de YouTube pese a
  las cookies (paso 7).
- **`Fallo el lote de traducción`** -- si aparece más seguido que en
  desarrollo, la IP del datacenter puede estar gatillando el throttling
  de Google Translate con más facilidad que tu IP residencial. El
  backoff ya está para cubrir esto, pero si se repite mucho, considerá
  bajar `SUBGEN_TRANSLATE_CONCURRENCY` de 2 a 1.
- **CPU sostenido alto en un worker sin ningún job activo en la UI** --
  ver "Problemas conocidos" en `ARCHITECTURE.md`. La mitigación
  (`SUBGEN_CELERY_MAX_TASKS_PER_CHILD=1`) ya viene activada por default,
  pero confirmá con `docker stats` que no vuelve a pasar.
- **Espacio en disco** -- `df -h` en cada worker, sobre todo después de
  varios jobs. Ver la sección de limpieza en `ARCHITECTURE.md`.

## Medir en producción, apenas esté arriba

Todas las decisiones de rendimiento de este proyecto (códec, fps de
salida, preset/crf, traducción concurrente, y CCX vs CPX) estaban
basadas originalmente en mediciones sobre **una máquina de desarrollo de
32 cores**, no en el hardware real de producción. **Esto ya se midió
-- ver "Resultados reales" más abajo.** El burn distribuido, el batched
de Whisper, y la causa raíz de los threads huérfanos siguen pendientes
de retomar (ver `ARCHITECTURE.md`).

### Resultados reales (CPX32, 4 vCPU compartidos, agosto 2026)

Tres jobs completos de producción, videos reales de YouTube, sin
optimizaciones adicionales más allá de lo ya documentado:

| Duración video | download | transcribe | translate | burn | TOTAL | fps burn |
|---|---|---|---|---|---|---|---|
| 9.3 min | 41.9s | 96.9s | 112.3s | 239.3s | 490.9s | ~95 |
| 37.9 min | 204.9s | 331.0s | 690.2s | 640.5s | 1867.9s | ~105 |
| 63.8 min | 132.4s | 656.4s | 683.7s | 1296.3s | 2771.8s | ~88 |

**Conclusión sobre CPX vs. CCX**: las tres corridas completaron sin
degradación errática ni caídas de rendimiento a mitad de job -- la
variación de `fps` de `burn` (42 a 105, según otra corrida intermedia
no tabulada arriba) se explica por **complejidad de contenido** (más
movimiento en cámara = más caro de codificar), no por steal time. No
hay evidencia en estos datos que justifique pagar el 2x de CCX por
ahora. Salvedad honesta: 3 corridas en una sesión no descartan
contención futura con otro inquilino del mismo host físico -- si en
producción sostenida empezás a ver tiempos erráticos entre corridas
similares, ahí sí sería la señal para reconsiderar.

**Hallazgo separado, real y consistente**: `translate` tardó
prácticamente lo mismo (690.2s y 683.7s) en dos videos de duración muy
distinta -- sugiere que se está pegando contra un techo de rate-limit
de Google Translate bastante estable desde esta IP de datacenter, no
ruido aleatorio. Si esto sigue así, vale la pena probar
`SUBGEN_TRANSLATE_CONCURRENCY=1` (en vez de 2) para ver si menos
paralelismo dispara menos el throttling en primer lugar.

**También encontrado, no anticipado originalmente**:
`SUBGEN_MAX_VIDEO_DURATION_SECONDS` (default 3600s/60min) y los límites
de tiempo de Celery necesitan subirse juntos si vas a aceptar videos
largos -- ver el incidente real documentado en `ARCHITECTURE.md`
"Problemas conocidos" (o el historial de esta sesión) para el cálculo
completo. Quedaron en `9000`/`9500`/`9800` respectivamente en esta
VPS -- ajustá según qué duración máxima quieras aceptar vos.

## Alternativa: si preferís el reparto original (1 servicio = 1 VPS)

Sigue siendo válido si por alguna razón preferís aislar cada servicio en
su propia VPS (por ejemplo, políticas de seguridad que exigen separación
física) -- simplemente corré `up -d api`, `up -d redis`, `up -d worker-N`
cada uno en su propia VPS en vez de agrupar `api redis` juntos. El
`docker-compose.prod.yml` funciona igual de cualquiera de las dos formas,
esto es una decisión de despliegue, no una restricción del archivo.
