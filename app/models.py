"""
Modelo de datos unificado de la API agnóstica.

Estos tipos son INDEPENDIENTES del proveedor. Cada adaptador (Docker local,
AWS RDS, GCP Cloud SQL...) es responsable de mapear estos conceptos a su
propio lenguaje. El cliente de la API solo habla en estos términos.

Ejemplo: el cliente pide `size=SMALL`. El adaptador local lo ignora; el
adaptador de AWS lo traducirá a `db.t3.micro`; el de GCP a `db-f1-micro`.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class DatabaseEngine(StrEnum):
    """Motores de BBDD soportados por la API."""

    POSTGRES = "postgres"
    MYSQL = "mysql"


class InstanceSize(StrEnum):
    """
    Tallas abstractas. Cada adaptador las mapea a su equivalente nativo.

    Es deliberado que NO expongamos `db.t3.micro` o equivalentes: ese tipo de
    detalle es lo que ata al proveedor y lo que esta API existe para abstraer.
    """

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class InstanceStatus(StrEnum):
    """Estados normalizados del ciclo de vida de una instancia."""

    CREATING = "creating"
    AVAILABLE = "available"
    STOPPED = "stopped"
    DELETING = "deleting"
    ERROR = "error"


class InstanceCreateRequest(BaseModel):
    """Payload que el cliente envía a POST /instances."""

    name: str = Field(..., min_length=3, max_length=63)
    engine: DatabaseEngine = DatabaseEngine.POSTGRES
    engine_version: str | None = None  # ej. "16", "15.3"; None = default del adaptador
    size: InstanceSize = InstanceSize.SMALL
    # Default seguro en TODOS los proveedores. "admin" está reservado en RDS
    # para Postgres como MasterUsername — el smoke test lo cazó. "dbadmin"
    # es libre en Postgres, MySQL, RDS, Docker. Si quieres otro, lo pasas.
    admin_username: str = "dbadmin"
    admin_password: str = Field(..., min_length=8)


class InstanceInfo(BaseModel):
    """Respuesta normalizada que la API devuelve sobre cualquier instancia."""

    id: str
    name: str
    engine: DatabaseEngine
    engine_version: str
    status: InstanceStatus
    host: str | None = None
    port: int | None = None
    provider: str  # ej. "local_docker", "aws_rds", "gcp_cloudsql"
