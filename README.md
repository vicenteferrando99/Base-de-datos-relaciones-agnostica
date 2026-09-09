# cloud-db-api

[![CI](https://github.com/vicenteferrando99/Base-de-datos-relaciones-agnostica/actions/workflows/ci.yml/badge.svg)](https://github.com/vicenteferrando99/Base-de-datos-relaciones-agnostica/actions/workflows/ci.yml)

API REST agnóstica de proveedor para gestionar bases de datos relacionales
en la nube (AWS, GCP) y en entornos locales (Docker), desarrollada como TFM
del Máster en Big Data, IA e Ingeniería de Datos (Universidad de Málaga).

**Tesis:** una aplicación cliente debería poder cambiar de proveedor de BBDD
modificando únicamente la configuración, sin tocar el código de negocio.

La API abstrae dos dimensiones ortogonales:

- **Control plane (proveedor):** aprovisionar/gestionar instancias es igual
  en Docker, AWS RDS y GCP Cloud SQL — cambia solo `PROVIDER`.
- **Data plane (motor):** un conjunto acotado de operaciones sobre datos
  (crear tabla, insertar, consultar) descrito en un modelo abstracto; cada
  motor (PostgreSQL, MySQL) tiene un dialecto que genera su SQL nativo.

## Estado actual

Adaptadores de proveedor (control plane):

- [x] `local_docker` — Postgres/MySQL en contenedores locales (CRUD completo).
- [x] `aws_rds` — Amazon RDS (CRUD completo, multi-región, smoke test real superado).
- [x] `gcp_cloudsql` — Google Cloud SQL (CRUD completo, aprovisionamiento
      diferido de BD y usuario, smoke test real superado).

Funcionalidad:

- [x] CRUD sobre `/instances` + `PUT /provider` (cambio de proveedor en caliente).
- [x] Registry plugin de adaptadores (añadir un proveedor no toca `main.py`).
- [x] Consola SQL (`POST /query`) agnóstica de proveedor, con driver por
      motor (psycopg / PyMySQL).
- [x] Capa de operaciones agnóstica de motor (`POST /data/tables|rows|select`):
      misma petición JSON → SQL nativo de PostgreSQL o MySQL. Ver
      [`docs/DATA_PLANE.md`](docs/DATA_PLANE.md).
- [x] Red automática: al crear una instancia cloud, la API autoriza su propia
      IP pública (Security Group en AWS, authorized networks en GCP) para que
      el data plane conecte sin pasos manuales.
- [x] UI web (Tailwind + Alpine, sin build): panel de instancias, consola SQL
      y constructor de operaciones que muestra el SQL generado por el dialecto.
- [x] Migración de datos entre instancias, entre proveedores y entre motores.
      Ver [`docs/DATA_PLANE.md`](docs/DATA_PLANE.md) §7.
- [x] Empaquetado en imagen Docker multi-stage (`Dockerfile`), con usuario sin
      privilegios.
- [x] 137 tests (modelo, dialectos, adaptadores con mocks, migración, endpoints,
      integración con Docker real) + CI en GitHub Actions.

## Requisitos

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) para gestión de dependencias
- Docker (Engine o Desktop) para el adaptador `local_docker`
- [`just`](https://github.com/casey/just) (opcional, recomendado) para
  atajos de tareas frecuentes
- Para AWS/GCP: credenciales configuradas fuera de la API
  (`aws configure` / `gcloud auth application-default login`); ver
  [`docs/SETUP.md`](docs/SETUP.md)

Desarrollado en Ubuntu y verificado también en Windows 11
(ver [`docs/SETUP.md`](docs/SETUP.md) §8).

## Puesta en marcha

```bash
# 1. Instalar dependencias
just install                            # o: uv sync

# 2. Configuración local  (`uv sync` NO crea el .env: es un paso manual)
cp .env.example .env                    # Windows: copy .env.example .env
# Edita .env: PROVIDER, y DOCKER_HOST solo si usas Docker Desktop en Linux.
# En Windows deja DOCKER_HOST sin definir. Ver docs/SETUP.md §3.

# 3. Lanzar la API en modo desarrollo
just run                                # o: uv run uvicorn app.main:app --reload
```

Una vez levantada:

- UI web: http://localhost:8000
- Swagger UI: http://localhost:8000/docs

### Ejecutar desde la imagen (opcional)

```bash
just docker-run
```

Monta el socket del daemon para que el adaptador local siga funcionando y
anuncia la puerta de enlace como host de las instancias: dentro del contenedor
`localhost` es el contenedor, no la máquina anfitriona.

## Ejemplo de uso

### Control plane (agnóstico de proveedor)

```bash
# Crear una instancia (en el proveedor activo, sea Docker, AWS o GCP)
curl -X POST http://localhost:8000/instances \
  -H "Content-Type: application/json" \
  -d '{"name":"ventas","admin_password":"mipassword123"}'

# Listar / borrar
curl http://localhost:8000/instances
curl -X DELETE http://localhost:8000/instances/<id>

# Cambiar de proveedor en caliente
curl -X PUT http://localhost:8000/provider \
  -H "Content-Type: application/json" -d '{"provider":"aws_rds"}'
```

### Data plane (agnóstico de motor)

La misma petición crea la tabla en PostgreSQL o en MySQL; el dialecto del
motor genera el SQL (que la respuesta incluye como evidencia):

```bash
curl -X POST http://localhost:8000/data/tables \
  -H "Content-Type: application/json" \
  -d '{
    "connection": {"engine":"postgres","host":"localhost","port":5432,
                   "user":"dbadmin","password":"mipassword123","dbname":"ventas"},
    "table": {"name":"prestamos","columns":[
      {"name":"id","type":"integer","primary_key":true,"nullable":false},
      {"name":"importe","type":"decimal"}]}
  }'
# -> {"sql": "CREATE TABLE \"prestamos\" (...)", ...}   (con engine=mysql: CREATE TABLE `prestamos` ...)
```

## Comandos comunes (justfile)

| Comando | Qué hace |
|---|---|
| `just` | Tests rápidos (sin Docker) |
| `just install` | `uv sync` — sincroniza el venv con el lockfile |
| `just run` | Lanza la API con autoreload |
| `just test` | Tests rápidos (lo mismo que `just`) |
| `just test-integration` | Tests que arrancan contenedores Postgres reales |
| `just test-all` | Toda la batería |
| `just lint` | `ruff check` + `ruff format --check` |
| `just format` | `ruff format` in-place |
| `just fix` | Lint con auto-fix + format |
| `just docker-clean` | Borra todos los contenedores creados por la API |
| `just clean` | Limpia caches locales |

Lista completa: `just --list`.

## Estructura

```
cloud-db-api/
├── app/
│   ├── main.py                # FastAPI: control plane (CRUD /instances, /provider) + UI
│   ├── config.py              # Settings: provider, docker_host, aws_*, gcp_*
│   ├── models.py              # Modelo unificado del control plane (Pydantic)
│   ├── network.py             # Detección de la IP pública (apertura de red automática)
│   ├── query.py               # Ejecución de SQL: driver por motor (psycopg/PyMySQL)
│   ├── adapters/              # CONTROL PLANE: un adaptador por PROVEEDOR
│   │   ├── base.py            #   contrato abstracto DatabaseAdapter
│   │   ├── registry.py        #   registry plugin (@register)
│   │   ├── local_docker.py    #   Docker local
│   │   ├── aws_rds.py         #   Amazon RDS
│   │   └── gcp_cloudsql.py    #   Google Cloud SQL
│   └── dataops/               # DATA PLANE: un dialecto por MOTOR
│       ├── models.py          #   modelo abstracto de operaciones (tabla/insert/select)
│       ├── dialects.py        #   SqlDialect: PostgresDialect, MySqlDialect
│       └── api.py             #   endpoints /query y /data/*
├── ui/index.html              # UI web (Tailwind + Alpine, sin build step)
├── tests/                     # 137 tests; ver conftest.py y fakes.py
├── docs/
│   ├── INFORME_DESARROLLO.md  # Crónica del desarrollo (bloques, decisiones, evidencia)
│   ├── GUIA_EJECUCION.md      # Cómo ejecutarlo y qué esperar en cada paso
│   ├── DATA_PLANE.md          # Diseño de la capa agnóstica de motor
│   └── SETUP.md               # Puesta en marcha de credenciales AWS/GCP
├── Dockerfile                 # Imagen de la API (multi-stage con uv)
├── scripts/demo_ingest.py     # PoC data plane provider-agnóstico
├── .github/workflows/ci.yml   # Pipeline CI (lint + tests rápidos)
├── justfile                   # Tareas comunes
├── LICENSE                    # MIT
└── pyproject.toml             # Deps, ruff, pytest
```

## Autor

Vicente Ferrando González — TFM 2026
Tutores: Daniel Garrido y José María Álvarez
