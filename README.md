# cloud-db-api

API REST agnóstica de proveedor para gestionar bases de datos relacionales
en la nube (AWS, GCP, Azure) y en entornos locales (Docker, on-prem),
desarrollada como TFM del Máster en Big Data, IA e Ingeniería de Datos
(Universidad de Málaga).

**Tesis:** una aplicación cliente debería poder cambiar de proveedor de BBDD
modificando únicamente la configuración, sin tocar el código de negocio.

## Estado actual

En desarrollo. Adaptadores planificados:

- [x] `local_docker` — Postgres/MySQL en contenedores locales (`create` listo)
- [ ] `aws_rds` — Amazon RDS
- [ ] `gcp_cloudsql` o `azure_db` — tercer adaptador (parcial, como prueba de extensibilidad)

## Requisitos

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) para gestión de dependencias
- Docker Desktop (o equivalente) para el adaptador local

## Puesta en marcha

```bash
# Instalar dependencias
uv sync

# Copiar configuración de ejemplo
cp .env.example .env

# Lanzar la API en modo desarrollo
uv run uvicorn app.main:app --reload

# Tests
uv run pytest
```

Una vez levantada, Swagger UI está en http://localhost:8000/docs

## Estructura

```
cloud-db-api/
├── app/
│   ├── main.py              # FastAPI app y endpoints
│   ├── models.py            # Modelo de datos unificado (Pydantic)
│   ├── config.py            # Configuración (qué adaptador usar)
│   └── adapters/
│       ├── base.py          # Contrato abstracto del adaptador
│       └── local_docker.py  # Adaptador Docker local
├── tests/
├── pyproject.toml
└── CLAUDE.md                # Contexto para Claude Code
```

## Autor

Vicente Ferrando González — TFM 2026
Tutores: Daniel Garrido y José María Álvarez
