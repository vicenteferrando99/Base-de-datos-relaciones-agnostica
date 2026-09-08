"""
Adaptador para Google Cloud SQL. Tercer adaptador del TFM, deliberadamente
escrito como ESPEJO de `aws_rds.py`: misma estructura, mismos helpers, mismo
patrón de ownership. Esa simetría es el argumento central de extensibilidad
del proyecto — añadir un proveedor nuevo es "rellenar la misma plantilla".

Decisiones de diseño y diferencias de capacidad frente a otros proveedores
(material directo para la "matriz de capacidades" de la memoria):

- **Credenciales**: NO se gestionan aquí. La librería de Google las lee de
  las Application Default Credentials (`gcloud auth application-default
  login`). Mismo principio que boto3 con `~/.aws/credentials`.

- **API**: se usa la Cloud SQL Admin API vía `google-api-python-client`
  (discovery del servicio `sqladmin` v1beta4). No hay un cliente tan
  idiomático como boto3, pero el discovery es estable y bien documentado.

- **`admin_username` se IGNORA**: a diferencia de RDS (donde `MasterUsername`
  es libre), Cloud SQL fija el usuario administrador por motor (`postgres`
  para PostgreSQL, `root` para MySQL). Solo se puede fijar su contraseña vía
  `rootPassword`. Esto es una limitación del proveedor — va a la matriz de
  capacidades, igual que `size` se ignora en Docker.

- **Ciclo de vida asíncrono**: como RDS, crear tarda minutos. `create_instance`
  devuelve `CREATING` con `host=port=None`; el cliente hace polling.

- **Ownership por label**: Cloud SQL soporta `userLabels` en la instancia.
  Usamos `managed-by=cloud-db-api` + `instance-id=<uuid>`. Mismo patrón que
  los labels de Docker y los tags de RDS. Nota: los labels de GCP solo
  admiten minúsculas, dígitos, guiones y guiones bajos.

- **Red automática**: Cloud SQL cierra la IP pública por defecto. Al crear,
  se añade la IP pública de esta API a `authorizedNetworks` (con `/32`), el
  equivalente GCP del Security Group de AWS pero por instancia (no hay
  recurso separado que crear: una clave más en el body). Si no se puede
  detectar la IP, la instancia nace con la red cerrada y se avisa en `note`.
"""

import logging
import threading
import time
import uuid

import google.auth
import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import network
from app.adapters.base import DatabaseAdapter
from app.adapters.registry import register
from app.config import settings
from app.models import (
    DatabaseEngine,
    InstanceCreateRequest,
    InstanceInfo,
    InstanceRegion,
    InstanceSize,
    InstanceStatus,
)

logger = logging.getLogger(__name__)

# --- Mapeos abstractos -> nativos de Cloud SQL ---

# Tier (tamaño de máquina). db-f1-micro es el shared-core más barato.
SIZE_TO_TIER: dict[InstanceSize, str] = {
    InstanceSize.SMALL: "db-f1-micro",
    InstanceSize.MEDIUM: "db-g1-small",
    InstanceSize.LARGE: "db-n1-standard-1",
}

# Región abstracta -> región nativa de GCP. A diferencia de RDS, Cloud SQL
# lista por proyecto (no por región), así que list/get/delete no necesitan
# iterar regiones; solo create usa la región.
REGION_MAP: dict[InstanceRegion, str] = {
    InstanceRegion.EUROPE: "europe-west1",
    InstanceRegion.US: "us-east1",
}

# Almacenamiento por defecto (GB) si el cliente no especifica.
DEFAULT_STORAGE_GB = 10

# databaseVersion en la API de Cloud SQL.
ENGINE_TO_DATABASE_VERSION: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRES: "POSTGRES_16",
    DatabaseEngine.MYSQL: "MYSQL_8_0",
}

# Puerto estándar por motor (Cloud SQL no expone puerto configurable).
ENGINE_TO_PORT: dict[DatabaseEngine, int] = {
    DatabaseEngine.POSTGRES: 5432,
    DatabaseEngine.MYSQL: 3306,
}

# Reverso para reconstruir el engine del modelo a partir del databaseVersion.
_DATABASE_VERSION_PREFIX_TO_ENGINE = {
    "POSTGRES": DatabaseEngine.POSTGRES,
    "MYSQL": DatabaseEngine.MYSQL,
}

# Labels de ownership (mismo patrón que Docker/RDS).
OWNER_LABEL_KEY = "managed-by"
OWNER_LABEL_VALUE = "cloud-db-api"

# Mapeo de state (nativo Cloud SQL) -> InstanceStatus (modelo unificado).
_GCP_STATE_MAP: dict[str, InstanceStatus] = {
    "PENDING_CREATE": InstanceStatus.CREATING,
    "MAINTENANCE": InstanceStatus.CREATING,
    "RUNNABLE": InstanceStatus.AVAILABLE,
    "SUSPENDED": InstanceStatus.STOPPED,
    "PENDING_DELETE": InstanceStatus.DELETING,
    "FAILED": InstanceStatus.ERROR,
}


def _gcp_state_to_status(raw: str) -> InstanceStatus:
    """Mapea state de Cloud SQL a InstanceStatus. Desconocido => CREATING.

    Conservador como en RDS: ante un estado no enumerado asumimos transición,
    no error.
    """
    return _GCP_STATE_MAP.get(raw, InstanceStatus.CREATING)


def _database_version_to_engine_and_version(
    database_version: str,
) -> tuple[DatabaseEngine, str]:
    """Parte 'POSTGRES_16' -> (DatabaseEngine.POSTGRES, '16')."""
    prefix, _, version = database_version.partition("_")
    engine = _DATABASE_VERSION_PREFIX_TO_ENGINE.get(prefix, DatabaseEngine.POSTGRES)
    return engine, version.replace("_", ".")


@register("gcp_cloudsql")
class GcpCloudSqlAdapter(DatabaseAdapter):
    """Adaptador de Google Cloud SQL para BBDD relacionales gestionadas."""

    def __init__(self, service=None) -> None:
        # `service` inyectable para tests (evita tocar la red). En producción
        # se construye con las Application Default Credentials.
        self.project = settings.gcp_project
        self._credentials = None
        # Estado del aprovisionamiento diferido (BD + usuario) por instance-id.
        # Vive en memoria: con un reinicio del proceso se pierde (limitación
        # conocida; la instancia seguiría usable con el usuario `postgres`).
        self._setup_status: dict[str, str | None] = {}
        if service is not None:
            self.service = service
            return
        if not self.project:
            raise RuntimeError("Falta configurar GCP_PROJECT en el entorno para usar gcp_cloudsql.")
        self._credentials, _ = google.auth.default()
        self.service = build(
            "sqladmin", "v1beta4", credentials=self._credentials, cache_discovery=False
        )

    def _execute(self, request):
        """Ejecuta una petición del cliente discovery de forma thread-safe.

        google-api-python-client usa httplib2 por debajo, que NO es thread-safe.
        FastAPI sirve los endpoints sync en un threadpool y la UI hace polling
        concurrente, así que compartir un único objeto HTTP entre hilos corrompe
        la conexión TLS ("record layer failure"). Por eso usamos un http
        autorizado nuevo en cada llamada. En tests (service inyectado, sin
        credenciales) se ejecuta directamente sobre el mock.
        """
        if self._credentials is None:
            return request.execute()
        http = google_auth_httplib2.AuthorizedHttp(self._credentials, http=httplib2.Http())
        return request.execute(http=http)

    # ---------- CREATE ----------
    def create_instance(self, request: InstanceCreateRequest) -> InstanceInfo:
        instance_id = uuid.uuid4().hex[:8]
        db_name = f"cloudapi-{request.name}-{instance_id}".lower()
        version = (
            f"{ENGINE_TO_DATABASE_VERSION[request.engine].split('_')[0]}_{request.engine_version}"
            if request.engine_version
            else ENGINE_TO_DATABASE_VERSION[request.engine]
        )

        # Red automática: autorizar la IP pública de la API. Sin IP detectable
        # la instancia nace con la red cerrada (mismo estado que antes de
        # automatizar esto) y se avisa vía `note`.
        ip = network.get_public_ip()
        ip_configuration: dict = {"ipv4Enabled": True}
        network_note = None
        if ip is not None:
            ip_configuration["authorizedNetworks"] = [
                {"name": "cloud-db-api-client", "value": f"{ip}/32"}
            ]
        else:
            network_note = (
                "No se pudo detectar la IP pública de la API: la red queda "
                "cerrada. Autoriza tu IP manualmente (ver docs/SETUP.md)."
            )

        body = {
            "name": db_name,
            "region": REGION_MAP[request.region],
            "databaseVersion": version,
            "rootPassword": request.admin_password,
            "settings": {
                # Edición ENTERPRISE (la clásica): es la única que admite los
                # tiers shared-core baratos (db-f1-micro). Por defecto Cloud SQL
                # crea en ENTERPRISE_PLUS, que solo acepta tiers caros
                # (db-perf-optimized-*). Lo fijamos para mantener el coste bajo.
                "edition": "ENTERPRISE",
                "tier": SIZE_TO_TIER[request.size],
                "dataDiskSizeGb": str(request.storage_gb or DEFAULT_STORAGE_GB),
                "userLabels": {
                    OWNER_LABEL_KEY: OWNER_LABEL_VALUE,
                    "instance-id": instance_id,
                    "instance-name": request.name.lower(),
                },
                "ipConfiguration": ip_configuration,
            },
        }

        self._execute(self.service.instances().insert(project=self.project, body=body))

        # Aprovisionamiento diferido (B6 + B7): Cloud SQL no permite crear la
        # BD ni el usuario hasta que la instancia esté RUNNABLE (minutos
        # después). Lanzamos un hilo que espera y los crea, para que de cara
        # al cliente todo nazca de una sola llamada. El resultado se refleja
        # en `note` vía el polling (GET). Solo en modo real (con credenciales);
        # en tests se invoca `_post_provision` directamente.
        if self._credentials is not None:
            self._setup_status[instance_id] = "Preparando base de datos y usuario..."
            threading.Thread(
                target=self._post_provision,
                args=(
                    instance_id,
                    db_name,
                    request.name.lower(),
                    request.admin_username,
                    request.admin_password,
                ),
                daemon=True,
            ).start()

        # Cloud SQL crea de forma asíncrona: devolvemos CREATING y el cliente
        # hará polling con GET /instances/{id}.
        notes = [n for n in (network_note, self._setup_status.get(instance_id)) if n]
        return InstanceInfo(
            id=instance_id,
            name=request.name,
            engine=request.engine,
            engine_version=request.engine_version
            or _database_version_to_engine_and_version(version)[1],
            status=InstanceStatus.CREATING,
            host=None,
            port=None,
            provider="gcp_cloudsql",
            database=request.name.lower(),  # la crea el setup diferido (B7)
            note=" · ".join(notes) or None,
        )

    # ---------- aprovisionamiento diferido (B6 + B7) ----------
    def _operations_in_progress(self, db_name: str) -> bool:
        """True si la instancia tiene alguna operación sin terminar.

        `state` y las *operaciones* son dos señales distintas en Cloud SQL: la
        instancia pasa a RUNNABLE mientras la operación de CREATE sigue en
        estado RUNNING (rematando backups, replicación, el patch de
        `authorizedNetworks`...). Consultar solo `state` es insuficiente.

        Degrada a False si la consulta falla o no devuelve un dict (caso de
        los tests, con el service mockeado): en ese escenario seguimos
        adelante y es `_execute_when_idle` quien absorbe el posible 409.
        """
        try:
            response = self._execute(
                self.service.operations().list(project=self.project, instance=db_name)
            )
            items = response.get("items", []) if isinstance(response, dict) else []
        except Exception:
            logger.debug("No se pudieron listar las operaciones de %s", db_name)
            return False
        return any(op.get("status") != "DONE" for op in items)

    def _wait_until_runnable(
        self, db_name: str, timeout_s: int = 900, interval_s: int = 20
    ) -> bool:
        """Sondea hasta que la instancia esté RUNNABLE y SIN operaciones en curso.

        Cloud SQL **serializa las operaciones que mutan una instancia**: si se
        lanza `databases().insert()` mientras queda una operación viva,
        responde `409 operationInProgress`. Por eso no basta con esperar a
        RUNNABLE — hay que esperar también a que se vacíe la cola.

        False si agota el tiempo.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            inst = self._execute(
                self.service.instances().get(project=self.project, instance=db_name)
            )
            if inst.get("state") == "RUNNABLE" and not self._operations_in_progress(db_name):
                return True
            time.sleep(interval_s)
        return False

    def _execute_when_idle(self, build_request, attempts: int = 10, delay_s: int = 15):
        """Ejecuta una petición reintentando ante `409 operationInProgress`.

        Red de seguridad para la carrera residual: entre que
        `_wait_until_runnable` da el visto bueno y nosotros escribimos, Cloud
        SQL puede arrancar una operación interna. `build_request` es un
        callable porque un objeto de petición del cliente discovery no debe
        reutilizarse entre intentos.
        """
        for attempt in range(1, attempts + 1):
            try:
                return self._execute(build_request())
            except HttpError as exc:
                conflict = exc.resp.status == 409
                if not conflict or attempt == attempts:
                    raise
                logger.info(
                    "Cloud SQL ocupada (409); reintento %d/%d en %ds",
                    attempt,
                    attempts,
                    delay_s,
                )
                time.sleep(delay_s)

    def _post_provision(
        self,
        instance_id: str,
        db_name: str,
        database_to_create: str,
        username: str,
        password: str,
    ) -> None:
        """Crea la BD y el usuario una vez la instancia está disponible.

        Registra el resultado en `self._setup_status[instance_id]`:
        - None => todo OK (sin nota).
        - texto => mensaje informativo o de error (lo muestra el GET en `note`).
        """
        try:
            if not self._wait_until_runnable(db_name):
                self._setup_status[instance_id] = (
                    "La instancia no llegó a estar lista a tiempo; BD y usuario no se crearon."
                )
                return

            # B7: base de datos con el nombre pedido.
            self._execute_when_idle(
                lambda: self.service.databases().insert(
                    project=self.project,
                    instance=db_name,
                    body={"name": database_to_create},
                )
            )

            # B6: usuario admin con el nombre pedido (Cloud SQL fija además
            # `postgres`/`root`; este se añade aparte).
            try:
                self._execute_when_idle(
                    lambda: self.service.users().insert(
                        project=self.project,
                        instance=db_name,
                        body={"name": username, "password": password},
                    )
                )
            except Exception as exc:
                logger.exception("Fallo creando el usuario en %s", db_name)
                self._setup_status[instance_id] = (
                    f"Base de datos creada, pero FALLÓ crear el usuario "
                    f"'{username}': {exc}. Puedes usar el usuario 'postgres'."
                )
                return

            self._setup_status[instance_id] = None  # todo correcto
        except Exception as exc:
            logger.exception("Fallo en el aprovisionamiento diferido de %s", db_name)
            self._setup_status[instance_id] = f"Fallo al preparar la base de datos: {exc}"

    # ---------- READ ----------
    def get_instance(self, instance_id: str) -> InstanceInfo:
        matches = self._find_by_id(instance_id)
        if not matches:
            raise LookupError(f"Instancia no encontrada: {instance_id}")
        return self._instance_to_info(matches[0])

    # ---------- LIST ----------
    def list_instances(self) -> list[InstanceInfo]:
        items = self._list_raw()
        return [
            self._instance_to_info(it)
            for it in items
            if (it.get("settings", {}).get("userLabels", {}) or {}).get(OWNER_LABEL_KEY)
            == OWNER_LABEL_VALUE
        ]

    # ---------- DELETE ----------
    def delete_instance(self, instance_id: str) -> None:
        """Idempotente: si no existe ninguna instancia con ese id, no hace nada."""
        for inst in self._find_by_id(instance_id):
            self._execute(
                self.service.instances().delete(project=self.project, instance=inst["name"])
            )

    # ---------- helpers ----------
    def _list_raw(self) -> list[dict]:
        """Todas las instancias Cloud SQL del proyecto (raw API). [] si ninguna."""
        response = self._execute(self.service.instances().list(project=self.project))
        return response.get("items", [])

    def _find_by_id(self, instance_id: str) -> list[dict]:
        """Instancias propias con label `instance-id=<id>`. [] si ninguna."""
        result = []
        for it in self._list_raw():
            labels = it.get("settings", {}).get("userLabels", {}) or {}
            if (
                labels.get(OWNER_LABEL_KEY) == OWNER_LABEL_VALUE
                and labels.get("instance-id") == instance_id
            ):
                result.append(it)
        return result

    def _instance_to_info(self, inst: dict) -> InstanceInfo:
        """Reconstruye un InstanceInfo a partir del response de Cloud SQL."""
        labels = inst.get("settings", {}).get("userLabels", {}) or {}
        engine, version = _database_version_to_engine_and_version(
            inst.get("databaseVersion", "POSTGRES_16")
        )
        instance_id = labels.get("instance-id", "")

        # IP pública primaria, si la instancia ya la tiene asignada.
        host = None
        for ip in inst.get("ipAddresses", []):
            if ip.get("type") == "PRIMARY":
                host = ip.get("ipAddress")
                break

        name = labels.get("instance-name", inst.get("name", ""))
        return InstanceInfo(
            id=instance_id,
            name=name,
            engine=engine,
            engine_version=version,
            status=_gcp_state_to_status(inst.get("state", "")),
            host=host,
            port=ENGINE_TO_PORT[engine] if host else None,
            provider="gcp_cloudsql",
            database=name or None,  # el setup diferido la crea con este nombre
            note=self._setup_status.get(instance_id),
        )
