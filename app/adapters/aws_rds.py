"""
Adaptador para Amazon RDS. Implementa el contrato `DatabaseAdapter` sobre
la API nativa de boto3.

Decisiones de diseño relevantes (apuntar para la memoria del TFM):

- **Credenciales**: NO se gestionan aquí. boto3 las lee automáticamente del
  fichero estándar `~/.aws/credentials`. Aquí solo recibimos parámetros NO
  secretos vía `Settings`. Esto evita duplicar el manejo de secretos y
  respeta el mecanismo nativo de AWS.

- **Ciclo de vida asíncrono**: a diferencia del adaptador local (donde
  el contenedor está listo en segundos), RDS tarda 5-15 minutos en pasar
  de `CREATING` a `AVAILABLE`. `create_instance` devuelve enseguida con
  `status=CREATING` y `host=port=None`. El cliente HTTP hace polling con
  `GET /instances/{id}`.

- **Multi-región**: RDS es un servicio *por región*. La región se elige por
  petición (`request.region`, abstracta) y se mapea a la región nativa. Como
  consecuencia, `list`/`get`/`delete` deben recorrer TODAS las regiones que
  soportamos (un cliente boto3 por región), porque `describe_db_instances`
  solo ve las instancias de su región. Con pocas regiones esto es barato.

- **Ownership por tag**: cada RDS lleva `managed-by=cloud-db-api` +
  `instance-id=<uuid>`. Misma idea que los `labels` de Docker.

- **Base de datos inicial (B7)**: pasamos `DBName` para que RDS cree una BD
  con el nombre pedido, igual que el adaptador Docker. Se sanea (los guiones
  no son válidos en nombres de BD de Postgres/MySQL).

- **Red automática**: para que el data plane pueda conectarse sin pasos
  manuales, `create_instance` (1) marca la instancia `PubliclyAccessible` y
  (2) garantiza un Security Group propio (`cloudapi-dbaccess`) en la Default
  VPC de la región con una regla de entrada para el puerto del motor desde
  la IP pública de esta API (`/32`, no `0.0.0.0/0`). El SG se reutiliza entre
  instancias y NO se borra al eliminar la última (es gratis y ahorra
  llamadas). Si `Settings.aws_security_group_id` está definido, se usa ese SG
  tal cual y no se toca ninguna regla. Si no se puede detectar la IP pública,
  la instancia se crea igual (red cerrada) y se avisa en `note`.
"""

import logging
import uuid

import boto3
from botocore.exceptions import ClientError

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

# --- Mapeos abstractos -> nativos de RDS ---

# Tipo de instancia: solo SMALL está dentro de free tier (db.t3.micro).
# MEDIUM y LARGE están definidos para mantener el contrato pero generan coste.
SIZE_TO_DB_INSTANCE_CLASS: dict[InstanceSize, str] = {
    InstanceSize.SMALL: "db.t3.micro",
    InstanceSize.MEDIUM: "db.t3.small",
    InstanceSize.LARGE: "db.t3.medium",
}

# Región abstracta -> región nativa de AWS.
REGION_MAP: dict[InstanceRegion, str] = {
    InstanceRegion.EUROPE: "eu-west-1",
    InstanceRegion.US: "us-east-1",
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

# Almacenamiento por defecto (GB) si el cliente no especifica.
DEFAULT_STORAGE_GB = 20

# Tags de ownership (mismo patrón que los labels del adaptador Docker).
OWNER_TAG_KEY = "managed-by"
OWNER_TAG_VALUE = "cloud-db-api"

# Puerto estándar de cada motor (para la regla de entrada del Security Group).
ENGINE_TO_PORT: dict[DatabaseEngine, int] = {
    DatabaseEngine.POSTGRES: 5432,
    DatabaseEngine.MYSQL: 3306,
}

# Security Group que la API crea (una vez por región) y reutiliza.
SECURITY_GROUP_NAME = "cloudapi-dbaccess"

logger = logging.getLogger(__name__)

# Mapeo de DBInstanceStatus (nativo RDS) -> InstanceStatus (modelo unificado).
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
    `storage-optimization`, etc.) que no merece la pena enumerar. Asumimos
    conservadoramente "en transición". Solo `failed` se considera ERROR.
    """
    return _RDS_STATUS_MAP.get(raw, InstanceStatus.CREATING)


def _sanitize_db_name(name: str) -> str:
    """Adapta el nombre a las reglas de DBName de RDS (sin guiones)."""
    return name.replace("-", "_")


@register("aws_rds")
class AwsRdsAdapter(DatabaseAdapter):
    """Adaptador de Amazon RDS para BBDD relacionales gestionadas."""

    def __init__(self) -> None:
        # Un cliente boto3 por servicio y región nativa, creado bajo demanda.
        # boto3 lee credenciales de ~/.aws/credentials automáticamente.
        # RDS para las instancias; EC2 solo para el Security Group (red).
        self._clients: dict[tuple[str, str], boto3.client] = {}

    def _client(self, native_region: str, service: str = "rds"):
        key = (service, native_region)
        if key not in self._clients:
            self._clients[key] = boto3.client(service, region_name=native_region)
        return self._clients[key]

    # ---------- CREATE ----------
    def create_instance(self, request: InstanceCreateRequest) -> InstanceInfo:
        instance_id = uuid.uuid4().hex[:8]
        db_identifier = f"cloudapi-{request.name}-{instance_id}"
        version = request.engine_version or DEFAULT_VERSIONS[request.engine]
        native_region = REGION_MAP[request.region]
        client = self._client(native_region)

        # Red automática: SG con la IP pública de la API autorizada. Si no se
        # pudo (sin internet / detección fallida), se crea sin SG propio y se
        # avisa en `note` — la instancia funciona, pero la red queda cerrada.
        security_group_id, note = self._ensure_network_access(
            native_region, ENGINE_TO_PORT[request.engine]
        )

        extra_kwargs = {}
        if security_group_id:
            extra_kwargs["VpcSecurityGroupIds"] = [security_group_id]

        client.create_db_instance(
            DBInstanceIdentifier=db_identifier,
            DBInstanceClass=SIZE_TO_DB_INSTANCE_CLASS[request.size],
            Engine=ENGINE_TO_RDS[request.engine],
            EngineVersion=version,
            MasterUsername=request.admin_username,
            MasterUserPassword=request.admin_password,
            AllocatedStorage=request.storage_gb or DEFAULT_STORAGE_GB,
            DBName=_sanitize_db_name(request.name),  # B7: BD inicial con tu nombre
            PubliclyAccessible=True,  # imprescindible para conectar desde fuera de la VPC
            Tags=[
                {"Key": OWNER_TAG_KEY, "Value": OWNER_TAG_VALUE},
                {"Key": "instance-id", "Value": instance_id},
                {"Key": "instance-name", "Value": request.name},
            ],
            **extra_kwargs,
        )

        # RDS tarda en estar disponible: devolvemos CREATING y el cliente
        # HTTP hará polling con GET /instances/{id}.
        return InstanceInfo(
            id=instance_id,
            name=request.name,
            engine=request.engine,
            engine_version=version,
            status=InstanceStatus.CREATING,
            host=None,
            port=None,
            provider="aws_rds",
            database=_sanitize_db_name(request.name),
            note=note,
        )

    # ---------- red (Security Group) ----------
    def _ensure_network_access(
        self, native_region: str, port: int
    ) -> tuple[str | None, str | None]:
        """Garantiza acceso de red para el data plane. Devuelve (sg_id, note).

        - Con `Settings.aws_security_group_id`: se respeta ese SG sin tocarlo.
        - Sin él: crea (si no existe) el SG `cloudapi-dbaccess` en la Default
          VPC de la región y autoriza el puerto del motor desde la IP pública
          de la API. Idempotente: la regla duplicada se ignora.
        - Si no hay IP pública detectable: (None, aviso para el usuario).
        """
        if settings.aws_security_group_id:
            return settings.aws_security_group_id, None

        ip = network.get_public_ip()
        if ip is None:
            return None, (
                "No se pudo detectar la IP pública de la API: la red queda "
                "cerrada. Abre el Security Group manualmente (ver docs/SETUP.md)."
            )

        ec2 = self._client(native_region, "ec2")
        sg_id = self._find_or_create_security_group(ec2)
        self._authorize_ingress(ec2, sg_id, port, f"{ip}/32")
        return sg_id, None

    @staticmethod
    def _find_or_create_security_group(ec2) -> str:
        """Id del SG `cloudapi-dbaccess` en la Default VPC; lo crea si falta."""
        found = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [SECURITY_GROUP_NAME]}]
        )["SecurityGroups"]
        if found:
            return found[0]["GroupId"]

        default_vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])[
            "Vpcs"
        ]
        if not default_vpc:
            raise RuntimeError(
                "No hay Default VPC en la región; configura AWS_SECURITY_GROUP_ID "
                "(y su VPC/subnets) manualmente."
            )
        created = ec2.create_security_group(
            GroupName=SECURITY_GROUP_NAME,
            Description="Acceso al data plane de cloud-db-api (gestionado por la API)",
            VpcId=default_vpc[0]["VpcId"],
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": OWNER_TAG_KEY, "Value": OWNER_TAG_VALUE}],
                }
            ],
        )
        return created["GroupId"]

    @staticmethod
    def _authorize_ingress(ec2, sg_id: str, port: int, cidr: str) -> None:
        """Añade la regla de entrada. Si ya existe (Duplicate), no pasa nada."""
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpProtocol="tcp",
                FromPort=port,
                ToPort=port,
                CidrIp=cidr,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise
            logger.debug("Regla %s:%s ya existente en %s", cidr, port, sg_id)

    # ---------- READ ----------
    def get_instance(self, instance_id: str) -> InstanceInfo:
        matches = self._find_dbs_by_id(instance_id)
        if not matches:
            raise LookupError(f"Instancia no encontrada: {instance_id}")
        client, db = matches[0]
        return self._db_to_info(client, db)

    # ---------- LIST ----------
    def list_instances(self) -> list[InstanceInfo]:
        results = []
        for client, db in self._iter_all_dbs():
            tags = self._tags_as_dict(client, db["DBInstanceArn"])
            if tags.get(OWNER_TAG_KEY) == OWNER_TAG_VALUE:
                results.append(self._db_to_info(client, db))
        return results

    # ---------- DELETE ----------
    def delete_instance(self, instance_id: str) -> None:
        """Idempotente: si no existe ninguna instancia con ese id, no hace nada.

        SkipFinalSnapshot=True para borrado limpio y rápido (sin snapshot).
        En producción muchas veces se quiere lo contrario; lo dejamos así por
        coste y para que la PoC del TFM sea efímera.
        """
        for client, db in self._find_dbs_by_id(instance_id):
            client.delete_db_instance(
                DBInstanceIdentifier=db["DBInstanceIdentifier"],
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )

    # ---------- helpers ----------
    def _iter_all_dbs(self):
        """Genera (client, db) para todas las instancias en TODAS las regiones.

        RDS es por región: hay que preguntar a cada una por separado.
        """
        for native_region in sorted(set(REGION_MAP.values())):
            client = self._client(native_region)
            for db in client.describe_db_instances()["DBInstances"]:
                yield client, db

    def _find_dbs_by_id(self, instance_id: str) -> list[tuple]:
        """Pares (client, db) propios con tag `instance-id=<id>`. [] si ninguno."""
        matches = []
        for client, db in self._iter_all_dbs():
            tags = self._tags_as_dict(client, db["DBInstanceArn"])
            if (
                tags.get(OWNER_TAG_KEY) == OWNER_TAG_VALUE
                and tags.get("instance-id") == instance_id
            ):
                matches.append((client, db))
        return matches

    @staticmethod
    def _tags_as_dict(client, arn: str) -> dict[str, str]:
        """Tags de un recurso RDS como dict {clave: valor}."""
        tag_list = client.list_tags_for_resource(ResourceName=arn)["TagList"]
        return {t["Key"]: t["Value"] for t in tag_list}

    def _db_to_info(self, client, db: dict) -> InstanceInfo:
        """Reconstruye un InstanceInfo a partir del response de RDS."""
        tags = self._tags_as_dict(client, db["DBInstanceArn"])
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
            database=db.get("DBName"),
        )
