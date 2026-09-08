# Atajos para tareas frecuentes del proyecto.
# Lista las recetas disponibles:  just --list
# Ejecuta la receta por defecto:  just
#
# MULTIPLATAFORMA: la mayoría de recetas son idénticas en Linux y Windows
# porque solo invocan `uv`, `docker` o `kubectl`. Las tres que dependen del
# sistema de ficheros o de los permisos POSIX (`clean`, `docker-clean` y
# `k8s-deploy`) están duplicadas con los atributos [unix] / [windows]: just
# elige la variante correcta según el sistema anfitrión.

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

# ---------- Fase 4: despliegue en Kubernetes local (kind + Helm) ----------

# Construye la imagen Docker de la API
docker-build:
    docker build -t cloud-db-api:0.1.0 .

# Crea el cluster kind (con el socket de Docker montado en el nodo)
k8s-up:
    kind create cluster --config deploy/kind-config.yaml

# Construye, carga la imagen en el nodo y despliega/actualiza con Helm.
# El gid del grupo docker del host se detecta y se pasa al chart para que
# el pod pueda usar el socket (supplementalGroups).
[doc("Construye, carga la imagen en kind y despliega/actualiza con Helm")]
[unix]
k8s-deploy: docker-build
    kind load docker-image cloud-db-api:0.1.0 --name cloud-db-api
    helm upgrade --install cloud-db-api deploy/helm/cloud-db-api \
        --set dockerSocket.groupId=$(getent group docker | cut -d: -f3)
    kubectl rollout status deployment/cloud-db-api-cloud-db-api --timeout=120s

# Variante Windows: no existe `getent` ni un grupo `docker` del host, así que
# no se puede calcular el gid. Se usa el valor por defecto del chart, que
# corresponde al socket dentro de la VM de Docker Desktop. Ver docs/SETUP.md §8.
[doc("Construye, carga la imagen en kind y despliega/actualiza con Helm")]
[windows]
k8s-deploy: docker-build
    kind load docker-image cloud-db-api:0.1.0 --name cloud-db-api
    helm upgrade --install cloud-db-api deploy/helm/cloud-db-api
    kubectl rollout status deployment/cloud-db-api-cloud-db-api --timeout=120s

# Acceso local a la API desplegada (UI en http://localhost:8000)
k8s-forward:
    kubectl port-forward svc/cloud-db-api-cloud-db-api 8000:8000

# Logs del pod de la API
k8s-logs:
    kubectl logs -l app.kubernetes.io/name=cloud-db-api -f

# Destruye el cluster kind completo
k8s-down:
    kind delete cluster --name cloud-db-api
