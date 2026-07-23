"""
Adaptador para Docker local. Es el primer adaptador del TFM y el más barato
de operar (no consume crédito cloud). Se usa también como banco de pruebas
del patrón de adaptadores antes de meterse con AWS o GCP.

Convención clave: TODOS los contenedores gestionados por esta API llevan el
label `managed-by=cloud-db-api`. Sin este label no podríamos distinguir
nuestros contenedores de otros que el usuario tenga en su Docker. Este patrón
de "ownership por label/tag" se replica idéntico en AWS (tags de recursos)
y GCP (labels), así que aprenderlo aquí es transferible.
"""

import uuid

import docker

from app.adapters.base import DatabaseAdapter
from app.adapters.registry import register
from app.config import settings
from app.models import (
    DatabaseEngine,
    InstanceCreateRequest,
    InstanceInfo,
    InstanceStatus,
)

# Mapeo motor -> imagen de Docker Hub. Los tags de versión se concatenan luego.
ENGINE_TO_IMAGE: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRES: "postgres",
    DatabaseEngine.MYSQL: "mysql",
}

# Versión por defecto si el cliente no especifica una
DEFAULT_VERSIONS: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRES: "16",
    DatabaseEngine.MYSQL: "8",
}

# Puerto interno por motor (dentro del contenedor). Lo mapeamos a un
# puerto aleatorio del host para permitir varias instancias simultáneas.
INTERNAL_PORTS: dict[DatabaseEngine, int] = {
    DatabaseEngine.POSTGRES: 5432,
    DatabaseEngine.MYSQL: 3306,
}

OWNER_LABEL_KEY = "managed-by"
OWNER_LABEL_VALUE = "cloud-db-api"

# Traducción entre estados nativos de Docker y los abstractos del modelo unificado.
# Vive a nivel de módulo (no como método) porque no depende del cliente Docker:
# es una función pura `str -> InstanceStatus`.
_DOCKER_STATUS_MAP: dict[str, InstanceStatus] = {
    "created": InstanceStatus.CREATING,
    "restarting": InstanceStatus.CREATING,
    "running": InstanceStatus.AVAILABLE,
    "paused": InstanceStatus.STOPPED,
    "exited": InstanceStatus.STOPPED,
    "dead": InstanceStatus.ERROR,
    "removing": InstanceStatus.DELETING,
}


def _docker_status_to_instance_status(raw: str) -> InstanceStatus:
    """Mapea estado nativo de Docker -> InstanceStatus. Desconocido => ERROR."""
    return _DOCKER_STATUS_MAP.get(raw, InstanceStatus.ERROR)


@register("local_docker")
class LocalDockerAdapter(DatabaseAdapter):
    """Implementa el contrato del adaptador usando contenedores Docker locales."""

    def __init__(self) -> None:
        # Si la configuración define una URL explícita de socket, la usamos
        # (caso típico: Docker Desktop en Linux, cuyo socket vive en
        # ~/.docker/desktop/docker.sock y no en /var/run/docker.sock).
        # Si no, delegamos en `from_env()` para que el SDK aplique su lógica
        # por defecto (variable de entorno DOCKER_HOST o socket clásico).
        if settings.docker_host:
            self.client = docker.DockerClient(base_url=settings.docker_host)
        else:
            self.client = docker.from_env()

    # ---------- CREATE ----------
    def create_instance(self, request: InstanceCreateRequest) -> InstanceInfo:
        instance_id = uuid.uuid4().hex[:8]
        version = request.engine_version or DEFAULT_VERSIONS[request.engine]
        image = f"{ENGINE_TO_IMAGE[request.engine]}:{version}"
        internal_port = INTERNAL_PORTS[request.engine]

        env = self._build_env(request)

        container = self.client.containers.run(
            image=image,
            name=f"cloudapi-{request.name}-{instance_id}",
            environment=env,
            ports={f"{internal_port}/tcp": None},  # None => puerto aleatorio en el host
            labels={
                OWNER_LABEL_KEY: OWNER_LABEL_VALUE,
                "instance-id": instance_id,
                "engine": request.engine.value,
                "engine-version": version,
                "instance-name": request.name,
            },
            detach=True,
        )

        # Releer para obtener el puerto que Docker asignó
        container.reload()
        host_port = container.ports[f"{internal_port}/tcp"][0]["HostPort"]

        return InstanceInfo(
            id=instance_id,
            name=request.name,
            engine=request.engine,
            engine_version=version,
            # Simplificación: el contenedor existe pero el motor tarda unos
            # segundos en estar listo. Más adelante implementaremos un
            # healthcheck real y devolveremos CREATING -> AVAILABLE.
            status=InstanceStatus.AVAILABLE,
            host=settings.docker_instance_host,
            port=int(host_port),
            provider="local_docker",
            database=request.name,  # POSTGRES_DB / MYSQL_DATABASE
        )

    # ---------- READ ----------
    def get_instance(self, instance_id: str) -> InstanceInfo:
        matches = self._find_by_id(instance_id)
        if not matches:
            raise LookupError(f"Instancia no encontrada: {instance_id}")
        return self._container_to_info(matches[0])

    # ---------- LIST ----------
    def list_instances(self) -> list[InstanceInfo]:
        containers = self.client.containers.list(
            filters={"label": f"{OWNER_LABEL_KEY}={OWNER_LABEL_VALUE}"},
            all=True,  # incluye parados, no solo "running"
        )
        return [self._container_to_info(c) for c in containers]

    # ---------- DELETE ----------
    def delete_instance(self, instance_id: str) -> None:
        """Idempotente: si no existe ningún contenedor con ese id, no hace nada."""
        for container in self._find_by_id(instance_id):
            container.remove(force=True)  # force=True para parar y borrar en un paso

    # ---------- helpers ----------
    def _find_by_id(self, instance_id: str) -> list:
        """Contenedores propios con `instance-id=<id>`. Lista vacía si ninguno."""
        return self.client.containers.list(
            filters={
                "label": [
                    f"{OWNER_LABEL_KEY}={OWNER_LABEL_VALUE}",
                    f"instance-id={instance_id}",
                ]
            },
            all=True,
        )

    @staticmethod
    def _container_to_info(container) -> InstanceInfo:
        """Reconstruye un InstanceInfo a partir de un contenedor del SDK de Docker."""
        labels = container.labels or {}
        engine = DatabaseEngine(labels.get("engine", DatabaseEngine.POSTGRES.value))
        internal_port = INTERNAL_PORTS[engine]

        # Si el contenedor está parado, ports puede no traer mapeo de host.
        mappings = (container.ports or {}).get(f"{internal_port}/tcp") or []
        host_port = (
            int(mappings[0]["HostPort"]) if mappings and mappings[0].get("HostPort") else None
        )

        name = labels.get("instance-name", container.name)
        return InstanceInfo(
            id=labels.get("instance-id", ""),
            name=name,
            engine=engine,
            engine_version=labels.get("engine-version", ""),
            status=_docker_status_to_instance_status(container.status),
            host=settings.docker_instance_host if host_port else None,
            port=host_port,
            provider="local_docker",
            database=name,  # la BD inicial se crea con el nombre de la instancia
        )

    @staticmethod
    def _build_env(request: InstanceCreateRequest) -> dict[str, str]:
        """Variables de entorno específicas según el motor."""
        if request.engine == DatabaseEngine.POSTGRES:
            return {
                "POSTGRES_USER": request.admin_username,
                "POSTGRES_PASSWORD": request.admin_password,
                "POSTGRES_DB": request.name,
            }
        # MySQL
        return {
            "MYSQL_ROOT_PASSWORD": request.admin_password,
            "MYSQL_DATABASE": request.name,
            "MYSQL_USER": request.admin_username,
            "MYSQL_PASSWORD": request.admin_password,
        }
