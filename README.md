# cloud-db-api

[![CI](https://github.com/vicenteferrando99/Base-de-datos-relaciones-agnostica/actions/workflows/ci.yml/badge.svg)](https://github.com/vicenteferrando99/Base-de-datos-relaciones-agnostica/actions/workflows/ci.yml)

API REST agnóstica de proveedor para gestionar bases de datos relacionales
en la nube (AWS, GCP, Azure) y en entornos locales (Docker), desarrollada
como TFM del Máster en Big Data, IA e Ingeniería de Datos (Universidad de
Málaga).

**Tesis:** una aplicación cliente debería poder cambiar de proveedor de BBDD
modificando únicamente la configuración, sin tocar el código de negocio.

## Estado actual

Adaptadores:

- [x] `local_docker` — Postgres/MySQL en contenedores locales (CRUD completo).
- [ ] `aws_rds` — Amazon RDS (en diseño).
- [ ] `gcp_cloudsql` o `azure_db` — tercer adaptador parcial (planificado).

Funcionalidad:

- [x] CRUD completo sobre `/instances` (POST, GET, GET/{id}, DELETE/{id}).
- [x] Registry plugin de adaptadores (añadir un proveedor no requiere
      tocar `main.py`).
- [x] 31 tests (modelo, helpers, integración con Docker real, endpoints
      HTTP con `FakeAdapter` inyectado).
- [x] Integración Continua en GitHub Actions (ruff + tests rápidos).

## Requisitos

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) para gestión de dependencias
- Docker (Engine o Desktop) para el adaptador `local_docker`
- [`just`](https://github.com/casey/just) (opcional, recomendado) para
  atajos de tareas frecuentes

## Puesta en marcha

```bash
# 1. Instalar dependencias
just install                            # o: uv sync

# 2. Configuración local
cp .env.example .env
# Edita .env si usas Docker Desktop en Linux: descomenta y ajusta DOCKER_HOST
# para que apunte a $HOME/.docker/desktop/docker.sock.

# 3. Lanzar la API en modo desarrollo
just run                                # o: uv run uvicorn app.main:app --reload
```

Una vez levantada:

- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs

## Ejemplo de uso

```bash
# Crear una instancia
curl -X POST http://localhost:8000/instances \
  -H "Content-Type: application/json" \
  -d '{"name":"ventas","admin_password":"mipassword123"}'

# Listar instancias
curl http://localhost:8000/instances

# Borrar (idempotente)
curl -X DELETE http://localhost:8000/instances/<id>
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
│   ├── main.py                # FastAPI app + endpoints CRUD + get_adapter()
│   ├── config.py              # Settings: provider, docker_host
│   ├── models.py              # Modelo unificado (Pydantic)
│   └── adapters/
│       ├── __init__.py        # Importa los adaptadores para activar @register
│       ├── base.py            # Contrato abstracto DatabaseAdapter
│       ├── registry.py        # Registry plugin (@register + get_adapter_class)
│       └── local_docker.py    # Adaptador Docker local
├── tests/
│   ├── conftest.py            # Fixtures: client, fake_adapter, docker_adapter
│   ├── fakes.py               # FakeAdapter en memoria
│   ├── test_models.py
│   ├── test_local_docker.py   # Unitarios + integración (Docker)
│   └── test_api.py            # Endpoints HTTP con FakeAdapter inyectado
├── .github/workflows/ci.yml   # Pipeline CI (lint + tests rápidos)
├── justfile                   # Tareas comunes
├── pyproject.toml             # Deps, ruff, pytest
└── CLAUDE.md                  # Contexto del proyecto para sesiones de Claude Code
```

## Autor

Vicente Ferrando González — TFM 2026
Tutores: Daniel Garrido y José María Álvarez
