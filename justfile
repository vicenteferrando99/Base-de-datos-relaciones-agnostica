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

# Borra TODOS los contenedores creados por la API (label managed-by=cloud-db-api)
docker-clean:
    -docker rm -f $(docker ps -aq --filter "label=managed-by=cloud-db-api") 2>/dev/null

# Limpia caches locales (no afecta a .venv ni a git)
clean:
    rm -rf .pytest_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +

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
k8s-deploy: docker-build
    kind load docker-image cloud-db-api:0.1.0 --name cloud-db-api
    helm upgrade --install cloud-db-api deploy/helm/cloud-db-api \
        --set dockerSocket.groupId=$(getent group docker | cut -d: -f3)
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
