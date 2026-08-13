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

## Por qué CCX y no CPX para los workers

Hetzner Cloud ofrece dos líneas de instancia relevantes acá:

- **CPX** (vCPU compartido): varios clientes comparten los mismos cores
  físicos, con el hypervisor repartiendo tiempo de CPU entre todos. Más
  barato por core nominal.
- **CCX** (vCPU dedicado): los cores están reservados en exclusiva para
  vos, sin compartir con otros clientes del mismo host físico.

La diferencia importa específicamente por el patrón de carga de este
proyecto: transcripción y quemado son procesos que sostienen **CPU al
100% durante minutos u horas**, no ráfagas cortas. En un vCPU compartido,
si otro cliente en el mismo host físico también está exigiendo CPU al
mismo tiempo, el hypervisor le "roba" ciclos a tu VM para dárselos al
otro inquilino -- esto se mide como **steal time**, y es exactamente el
escenario donde más duele: cargas sostenidas y prolongadas, no picos
breves que el scheduler puede absorber fácil. Con CCX no hay ese riesgo,
los cores son tuyos todo el tiempo.

Para `api`/`redis`/`minio` (la VPS chica), CPX alcanza de sobra -- ahí el
patrón de carga es exactamente el que un vCPU compartido maneja bien
(ráfagas cortas de I/O, no cómputo sostenido).

## Cómo desplegar

### 1. Elegí el tamaño de cada VPS

- **VPS combinada** (api+redis+minio): CPX21 o similar alcanza (2 vCPU
  compartidos, 4GB RAM) -- esta VPS no hace cómputo pesado.
- **VPS de worker**: CCX23 o superior (2 vCPU dedicados, 8GB RAM) --
  ajustá el tamaño según cuántos jobs concurrentes esperás por worker
  (con `concurrency=1`, es un job a la vez por VPS).

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
