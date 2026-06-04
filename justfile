# Atajos para tareas frecuentes del proyecto.
# Lista las recetas disponibles:  just --list
# Ejecuta la receta por defecto:  just

# Receta por defecto al ejecutar `just` sin argumentos
default: test

# Sincroniza el entorno virtual con pyproject.toml + uv.lock
install:
    uv sync

# Tests rápidos (sin Docker)
test:
    uv run pytest -m "not integration"

# Tests de integración con Docker real
test-integration:
    uv run pytest -m integration

# Toda la batería (rápidos + integración)
test-all:
    uv run pytest

# Lanza la API en modo desarrollo con autoreload
run:
    uv run uvicorn app.main:app --reload

# Linter (sin modificar)
lint:
    uv run ruff check .
    uv run ruff format --check .

# Formatea el código in-place
format:
    uv run ruff format .

# Aplica lint con auto-fix + formato
fix:
    uv run ruff check . --fix
    uv run ruff format .

# Borra TODOS los contenedores creados por la API (label managed-by=cloud-db-api)
docker-clean:
    -docker rm -f $(docker ps -aq --filter "label=managed-by=cloud-db-api") 2>/dev/null

# Limpia caches locales (no afecta a .venv ni a git)
clean:
    rm -rf .pytest_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
