# Atajos para tareas frecuentes del proyecto.
# Lista las recetas disponibles:  just --list
# Ejecuta la receta por defecto:  just
#
# MULTIPLATAFORMA: la mayoría de recetas son idénticas en Linux y Windows
# porque solo invocan `uv` o `docker`. Las dos que dependen del sistema de
# ficheros (`clean` y `docker-clean`) están duplicadas con los atributos
# [unix] / [windows]: just elige la variante correcta según el sistema
# anfitrión.

# En Windows just usa `cmd.exe` por defecto, que no entiende `$(...)`,
# `2>/dev/null` ni `rm`. Fijamos PowerShell para tener una sintaxis usable.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

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

# Lanza la API en modo desarrollo con autoreload (sirve también la UI en /)
run:
    uv run uvicorn app.main:app --reload

# Alias para arrancar la app y abrir directamente la UI en el navegador
ui:
    @echo "UI: http://localhost:8000  ·  API docs: http://localhost:8000/docs"
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

[doc("Borra TODOS los contenedores creados por la API (label managed-by=cloud-db-api)")]
[unix]
docker-clean:
    -docker rm -f $(docker ps -aq --filter "label=managed-by=cloud-db-api") 2>/dev/null

[doc("Borra TODOS los contenedores creados por la API (label managed-by=cloud-db-api)")]
[windows]
docker-clean:
    @$ids = docker ps -aq --filter "label=managed-by=cloud-db-api"; if ($ids) { docker rm -f $ids } else { Write-Output "Sin contenedores de la API que borrar." }

[doc("Limpia caches locales (no afecta a .venv ni a git)")]
[unix]
clean:
    rm -rf .pytest_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +

[doc("Limpia caches locales (no afecta a .venv ni a git)")]
[windows]
clean:
    @foreach ($d in '.pytest_cache', '.ruff_cache') { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
    @Get-ChildItem -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notlike '*\.venv\*' } | Remove-Item -Recurse -Force

# ---------- Empaquetado ----------

# Construye la imagen Docker de la API
docker-build:
    docker build -t cloud-db-api:0.1.0 .

# Monta el socket del daemon para que el adaptador local siga funcionando desde
# dentro del contenedor, y anuncia la puerta de enlace como host de las
# instancias: dentro del contenedor "localhost" es el propio contenedor, no la
# máquina anfitriona.
[doc("Ejecuta la API desde la imagen Docker")]
docker-run: docker-build
    docker run --rm -p 8000:8000 \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -e DOCKER_INSTANCE_HOST=172.17.0.1 \
        cloud-db-api:0.1.0
