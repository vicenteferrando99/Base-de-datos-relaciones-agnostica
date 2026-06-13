"""
Adaptador para Amazon RDS. Implementa el contrato `DatabaseAdapter` sobre
la API nativa de boto3.

Decisiones de diseño relevantes (apuntar para la memoria del TFM):

- **Credenciales**: NO se gestionan aquí. boto3 las lee automáticamente del
  fichero estándar `~/.aws/credentials`. Aquí solo recibimos parámetros NO
  secretos vía `Settings` (región, overrides de red). Esto evita duplicar
  el manejo de secretos y respeta el mecanismo nativo de AWS.

- **Ciclo de vida asíncrono**: a diferencia del adaptador local (donde
  el contenedor está listo en segundos), RDS tarda 5-15 minutos en pasar
  de `CREATING` a `AVAILABLE`. `create_instance` devuelve enseguida con
  `status=CREATING` y `host=port=None`. El cliente HTTP debe hacer polling
  con `GET /instances/{id}` hasta que `status==AVAILABLE` y aparezcan
  `host:port`.

- **Red (VPC)**: por defecto se descubre la *Default VPC* de la cuenta y
  se reutiliza su Security Group por defecto. Si el usuario fija
  `AWS_DB_SUBNET_GROUP` y/o `AWS_SECURITY_GROUP_ID` en el `.env`, esos
  overrides toman precedencia. Esto cubre el caso típico (cuenta limpia)
  sin renunciar al caso profesional (red corporativa custom).

- **Ownership por tag**: replicamos el patrón del adaptador local. Cada
  RDS lleva el tag `managed-by=cloud-db-api` + `instance-id=<uuid>`.
  Es la misma idea que `labels` en Docker, transferible.
"""

import uuid

import boto3

from app.adapters.base import DatabaseAdapter
from app.adapters.registry import register
from app.config import settings
from app.models import (
    DatabaseEngine,
    InstanceCreateRequest,
    InstanceInfo,
    InstanceSize,
    InstanceStatus,
)

# --- Mapeos abstractos -> nativos de RDS ---

# Tipo de instancia: solo SMALL está dentro de free tier (db.t3.micro).
# MEDIUM y LARGE están definidos para mantener el contrato pero generan coste.
SIZE_TO_DB_INSTANCE_CLASS: dict[InstanceSize, str] = {
    InstanceSize.SMALL: "db.t3.micro",
    InstanceSize.MEDIUM: "db.t3.small",
    InstanceSize.LARGE: "db.t3.medium",
}

# Nombre del motor en la API de RDS.
ENGINE_TO_RDS: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRES: "postgres",
    DatabaseEngine.MYSQL: "mysql",
}

# Versiones por defecto si el cliente no especifica una.
# AWS retira versiones antiguas con cierta frecuencia; conviene revisar
# periódicamente con `aws rds describe-db-engine-versions --engine <X>`.
# Validado en eu-west-1 a junio 2026.
DEFAULT_VERSIONS: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRES: "16.14",
    DatabaseEngine.MYSQL: "8.0.46",
}

# Tags de ownership (mismo patrón que los labels del adaptador Docker).
OWNER_TAG_KEY = "managed-by"
OWNER_TAG_VALUE = "cloud-db-api"

# Mapeo de DBInstanceStatus (nativo RDS) -> InstanceStatus (modelo unificado).
# Lista parcial pero cubre los estados habituales del ciclo de vida.
_RDS_STATUS_MAP: dict[str, InstanceStatus] = {
    "creating": InstanceStatus.CREATING,
    "backing-up": InstanceStatus.CREATING,
    "modifying": InstanceStatus.CREATING,
    "starting": InstanceStatus.CREATING,
    "available": InstanceStatus.AVAILABLE,
    "stopped": InstanceStatus.STOPPED,
    "stopping": InstanceStatus.STOPPED,
    "deleting": InstanceStatus.DELETING,
    "failed": InstanceStatus.ERROR,
}


def _rds_status_to_instance_status(raw: str) -> InstanceStatus:
    """Mapea DBInstanceStatus de RDS a InstanceStatus.

    Estados desconocidos => CREATING. RDS tiene muchos estados intermedios
    durante el aprovisionamiento (`configuring-enhanced-monitoring`,
    `storage-optimization`, etc.) que no merece la pena enumerar todos.
    Asumimos conservadoramente "en transición" en vez de ERROR. Solo
    `failed` se considera ERROR explícitamente vía el mapeo.
    """
    return _RDS_STATUS_MAP.get(raw, InstanceStatus.CREATING)


@register("aws_rds")
class AwsRdsAdapter(DatabaseAdapter):
    """Adaptador de Amazon RDS para BBDD relacionales gestionadas."""

    def __init__(self) -> None:
        # boto3 lee credenciales de ~/.aws/credentials automáticamente;
        # aquí solo le pasamos la región (que ya viene de Settings).
        self.client = boto3.client("rds", region_name=settings.aws_region)

    # ---------- CREATE ----------
    def create_instance(self, request: InstanceCreateRequest) -> InstanceInfo:
        instance_id = uuid.uuid4().hex[:8]
        db_identifier = f"cloudapi-{request.name}-{instance_id}"
        version = request.engine_version or DEFAULT_VERSIONS[request.engine]

        self.client.create_db_instance(
            DBInstanceIdentifier=db_identifier,
            DBInstanceClass=SIZE_TO_DB_INSTANCE_CLASS[request.size],
            Engine=ENGINE_TO_RDS[request.engine],
            EngineVersion=version,
            MasterUsername=request.admin_username,
            MasterUserPassword=request.admin_password,
            AllocatedStorage=20,  # mínimo razonable para Postgres/MySQL en RDS
            Tags=[
                {"Key": OWNER_TAG_KEY, "Value": OWNER_TAG_VALUE},
                {"Key": "instance-id", "Value": instance_id},
                {"Key": "instance-name", "Value": request.name},
            ],
        )

        # RDS tarda 5-15 min en estar disponible: devolvemos CREATING y el
        # cliente HTTP hará polling con GET /instances/{id}.
        return InstanceInfo(
            id=instance_id,
            name=request.name,
            engine=request.engine,
            engine_version=version,
            status=InstanceStatus.CREATING,
            host=None,
            port=None,
            provider="aws_rds",
        )

    # ---------- READ ----------
    def get_instance(self, instance_id: str) -> InstanceInfo:
        matches = self._find_dbs_by_id(instance_id)
        if not matches:
            raise LookupError(f"Instancia no encontrada: {instance_id}")
        return self._db_to_info(matches[0])

    # ---------- LIST ----------
    def list_instances(self) -> list[InstanceInfo]:
        response = self.client.describe_db_instances()
        results = []
        for db in response["DBInstances"]:
            tags = self._tags_as_dict(db["DBInstanceArn"])
            if tags.get(OWNER_TAG_KEY) == OWNER_TAG_VALUE:
                results.append(self._db_to_info(db))
        return results

    # ---------- DELETE ----------
    def delete_instance(self, instance_id: str) -> None:
        """Idempotente: si no existe ninguna instancia con ese id, no hace nada.

        SkipFinalSnapshot=True para borrado limpio y rápido (sin snapshot).
        En producción muchas veces se quiere lo contrario; lo dejamos así por
        coste y para que la PoC del TFM sea efímera. Apuntar en la memoria.
        """
        for db in self._find_dbs_by_id(instance_id):
            self.client.delete_db_instance(
                DBInstanceIdentifier=db["DBInstanceIdentifier"],
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )

    # ---------- helpers ----------
    def _find_dbs_by_id(self, instance_id: str) -> list[dict]:
        """DBs propias con tag `instance-id=<id>`. Lista vacía si ninguna.

        RDS no permite filtrar por tag directamente en describe_db_instances;
        iteramos y filtramos en memoria. Aceptable para nuestro volumen.
        """
        response = self.client.describe_db_instances()
        matches = []
        for db in response["DBInstances"]:
            tags = self._tags_as_dict(db["DBInstanceArn"])
            if (
                tags.get(OWNER_TAG_KEY) == OWNER_TAG_VALUE
                and tags.get("instance-id") == instance_id
            ):
                matches.append(db)
        return matches

    def _tags_as_dict(self, arn: str) -> dict[str, str]:
        """Devuelve los tags de un recurso RDS como dict {clave: valor}."""
        tag_list = self.client.list_tags_for_resource(ResourceName=arn)["TagList"]
        return {t["Key"]: t["Value"] for t in tag_list}

    def _db_to_info(self, db: dict) -> InstanceInfo:
        """Reconstruye un InstanceInfo a partir del response de RDS."""
        tags = self._tags_as_dict(db["DBInstanceArn"])
        endpoint = db.get("Endpoint") or {}
        return InstanceInfo(
            id=tags.get("instance-id", ""),
            name=tags.get("instance-name", ""),
            engine=DatabaseEngine(db["Engine"]),
            engine_version=db.get("EngineVersion", ""),
            status=_rds_status_to_instance_status(db.get("DBInstanceStatus", "")),
            host=endpoint.get("Address"),
            port=endpoint.get("Port"),
            provider="aws_rds",
        )
