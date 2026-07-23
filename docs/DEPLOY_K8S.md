# Despliegue en Kubernetes local (kind + Helm) — Fase 4

> Kubernetes actúa aquí SOLO como plataforma de despliegue de la API (alcance
> acordado del TFM). Las instancias de BBDD siguen viviendo en su proveedor
> (Docker del host, AWS, GCP), no en el cluster.

## Las piezas

| Pieza | Qué es | Fichero |
|---|---|---|
| Imagen Docker | La API empaquetada (multi-stage con `uv`, non-root) | `Dockerfile` |
| kind | Cluster Kubernetes real dentro de un contenedor Docker | `deploy/kind-config.yaml` |
| Helm chart | Plantillas de despliegue parametrizadas por `values.yaml` | `deploy/helm/cloud-db-api/` |

## Camino rápido (con just)

```bash
just k8s-up        # crea el cluster kind (una vez)
just k8s-deploy    # build imagen + carga en el nodo + helm upgrade --install
just k8s-forward   # port-forward: UI en http://localhost:8000
just k8s-logs      # logs del pod
just k8s-down      # destruye el cluster
```

Requisitos: `docker`, `kubectl`, `helm` y `kind` (binario único:
`curl -sLo ~/.local/bin/kind https://github.com/kubernetes-sigs/kind/releases/latest/download/kind-linux-amd64 && chmod +x ~/.local/bin/kind`).

## Camino manual (lo que hace `just` por dentro)

```bash
# 1. Imagen
docker build -t cloud-db-api:0.1.0 .

# 2. Cluster (el config monta el socket de Docker del host en el nodo)
kind create cluster --config deploy/kind-config.yaml

# 3. Cargar la imagen en el nodo (no hay registry: se copia a mano)
kind load docker-image cloud-db-api:0.1.0 --name cloud-db-api

# 4. Desplegar (el gid del grupo docker del host permite al pod usar el socket)
helm upgrade --install cloud-db-api deploy/helm/cloud-db-api \
    --set dockerSocket.groupId=$(getent group docker | cut -d: -f3)

# 5. Acceder
kubectl port-forward svc/cloud-db-api-cloud-db-api 8000:8000
# UI: http://localhost:8000 · Swagger: http://localhost:8000/docs
```

## Cómo funciona `local_docker` dentro del cluster

Dos problemas y sus soluciones (material para la memoria):

1. **El pod necesita hablar con el daemon de Docker del host.** Cadena de
   montajes: host → (`extraMounts` de kind) → nodo → (`hostPath` del chart) →
   pod. El pod NO corre como root: se une al grupo `docker` del host vía
   `supplementalGroups` (por eso hay que pasar el gid en `values`).
2. **"localhost" dentro del pod es el pod, no el host.** Las instancias se
   publican en puertos del host, así que el chart fija
   `DOCKER_INSTANCE_HOST=172.18.0.1` (la puerta de enlace de la red `kind`,
   que es el host visto desde dentro). El adaptador anuncia ese host en los
   datos de conexión (`Settings.docker_instance_host`, default `localhost`).

## Proveedores cloud desde el cluster

Las credenciales NUNCA van en la imagen ni en el chart: se crean como
Secrets y el chart los monta si se le da el nombre.

```bash
# AWS (boto3 leerá /home/apiuser/.aws/credentials en el pod)
kubectl create secret generic aws-credentials \
    --from-file=credentials=$HOME/.aws/credentials

# GCP (Application Default Credentials + GOOGLE_APPLICATION_CREDENTIALS)
kubectl create secret generic gcp-credentials \
    --from-file=credentials.json=$HOME/.config/gcloud/application_default_credentials.json

helm upgrade --install cloud-db-api deploy/helm/cloud-db-api \
    --set dockerSocket.groupId=$(getent group docker | cut -d: -f3) \
    --set credentials.aws.secretName=aws-credentials \
    --set credentials.gcp.secretName=gcp-credentials \
    --set credentials.gcp.project=<TU_PROYECTO>
```

Cambiar el proveedor por defecto: `--set provider=aws_rds` (o en caliente
desde la UI, como siempre).

## Decisiones y limitaciones conscientes

- **`replicaCount: 1` fijo.** La API tiene estado de proceso (proveedor
  activo cambiado en caliente, caché de adaptadores, aprovisionamiento
  diferido de GCP). Escalar horizontalmente requeriría externalizar ese
  estado — fuera del alcance del TFM y anotado como trabajo futuro.
- **Sin registry.** La imagen se carga con `kind load docker-image`
  (suficiente para un cluster local; en producción habría un registry).
- **Probes a `/health`** (readiness y liveness) y recursos acotados.
- El montaje del socket de Docker da al pod control total sobre el Docker
  del host: aceptable en un cluster local de desarrollo, inaceptable en
  producción (ahí solo se usarían los proveedores cloud, con
  `dockerSocket.enabled=false`).

## Verificado (jul 2026)

Flujo completo contra la API desplegada en kind: `POST /instances` creó un
Postgres en el daemon del host a través del socket montado; el pod se conectó
a él vía `172.18.0.1:<puerto>`; las operaciones agnósticas de motor
(`/data/tables`, `/data/rows`, `/data/select`) funcionaron; `DELETE` limpió.
