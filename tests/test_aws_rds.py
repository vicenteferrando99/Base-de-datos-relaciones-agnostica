"""
Tests del adaptador AwsRdsAdapter usando moto para mockear la API de AWS.

Moto intercepta las llamadas HTTP que boto3 enviaría a AWS y las responde
con estado simulado en memoria. Resultado: tests instantáneos, gratis, sin
internet y deterministas. NO sustituyen al smoke test final contra AWS real,
pero cubren el camino feliz y la mayoría de casos de error.
"""

import boto3
import pytest
from moto import mock_aws

from app.adapters.aws_rds import AwsRdsAdapter
from app.models import DatabaseEngine, InstanceCreateRequest, InstanceStatus


@pytest.fixture(autouse=True)
def _aws_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credenciales dummy para que boto3 no busque las reales del sistema."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")


@mock_aws
def test_create_instance_returns_basic_fields() -> None:
    adapter = AwsRdsAdapter()
    request = InstanceCreateRequest(
        name="testdb",
        admin_password="supersecret123",
    )

    info = adapter.create_instance(request)

    assert info.name == "testdb"
    assert info.engine == DatabaseEngine.POSTGRES
    assert info.provider == "aws_rds"
    assert info.id  # no vacío
    assert info.status == InstanceStatus.CREATING
    # Mientras la instancia se está creando, host/port aún no existen.
    assert info.host is None
    assert info.port is None


@mock_aws
def test_create_instance_calls_rds_with_correct_args() -> None:
    """Verifica los argumentos que llegaron a RDS consultando el estado."""
    adapter = AwsRdsAdapter()
    adapter.create_instance(InstanceCreateRequest(name="testdb", admin_password="supersecret123"))

    client = boto3.client("rds", region_name="eu-west-1")
    dbs = client.describe_db_instances()["DBInstances"]
    assert len(dbs) == 1
    db = dbs[0]
    assert db["DBInstanceClass"] == "db.t3.micro"
    assert db["Engine"] == "postgres"
    assert db["MasterUsername"] == "admin"
    assert db["AllocatedStorage"] == 20


@mock_aws
def test_create_instance_supports_mysql() -> None:
    adapter = AwsRdsAdapter()
    info = adapter.create_instance(
        InstanceCreateRequest(
            name="mysqldb",
            engine=DatabaseEngine.MYSQL,
            admin_password="supersecret123",
        )
    )

    client = boto3.client("rds", region_name="eu-west-1")
    db = client.describe_db_instances()["DBInstances"][0]
    assert db["Engine"] == "mysql"
    assert info.engine == DatabaseEngine.MYSQL


@mock_aws
def test_create_instance_uses_default_version_when_not_provided() -> None:
    """Si el cliente no pide versión, se usa la default del adaptador."""
    adapter = AwsRdsAdapter()
    info = adapter.create_instance(
        InstanceCreateRequest(name="testdb", admin_password="supersecret123")
    )
    # Default Postgres según DEFAULT_VERSIONS del adaptador.
    assert info.engine_version == "16.4"


@mock_aws
def test_create_instance_uses_custom_engine_version_when_provided() -> None:
    """Si el cliente pide una versión concreta, se respeta."""
    adapter = AwsRdsAdapter()
    info = adapter.create_instance(
        InstanceCreateRequest(
            name="testdb",
            engine_version="15.5",
            admin_password="supersecret123",
        )
    )
    assert info.engine_version == "15.5"


@mock_aws
def test_create_instance_tags_with_ownership() -> None:
    """Cada instancia lleva tags managed-by + instance-id + instance-name."""
    adapter = AwsRdsAdapter()
    info = adapter.create_instance(
        InstanceCreateRequest(name="testdb", admin_password="supersecret123")
    )

    client = boto3.client("rds", region_name="eu-west-1")
    db = client.describe_db_instances()["DBInstances"][0]
    tags = client.list_tags_for_resource(ResourceName=db["DBInstanceArn"])["TagList"]
    tag_dict = {t["Key"]: t["Value"] for t in tags}
    assert tag_dict["managed-by"] == "cloud-db-api"
    assert tag_dict["instance-id"] == info.id
    assert tag_dict["instance-name"] == "testdb"


# ------------------------- get_instance -------------------------


@mock_aws
def test_get_instance_returns_info_for_created_instance() -> None:
    adapter = AwsRdsAdapter()
    created = adapter.create_instance(
        InstanceCreateRequest(name="testdb", admin_password="supersecret123")
    )

    fetched = adapter.get_instance(created.id)

    assert fetched.id == created.id
    assert fetched.name == "testdb"
    assert fetched.engine == DatabaseEngine.POSTGRES
    assert fetched.provider == "aws_rds"


@mock_aws
def test_get_instance_raises_lookup_error_if_not_found() -> None:
    adapter = AwsRdsAdapter()
    with pytest.raises(LookupError):
        adapter.get_instance("noexiste-1234")


# ------------------------- list_instances -------------------------


@mock_aws
def test_list_empty_returns_empty_list() -> None:
    adapter = AwsRdsAdapter()
    assert adapter.list_instances() == []


@mock_aws
def test_list_returns_only_managed_instances() -> None:
    """Una instancia creada SIN nuestros tags no aparece en la lista."""
    adapter = AwsRdsAdapter()
    adapter.create_instance(InstanceCreateRequest(name="mine", admin_password="supersecret123"))

    # Creamos una instancia "ajena" directamente con boto3 (sin tags propios).
    client = boto3.client("rds", region_name="eu-west-1")
    client.create_db_instance(
        DBInstanceIdentifier="alien-db",
        DBInstanceClass="db.t3.micro",
        Engine="postgres",
        MasterUsername="root",
        MasterUserPassword="otherpassword",
        AllocatedStorage=20,
    )

    listed = adapter.list_instances()
    assert len(listed) == 1
    assert listed[0].name == "mine"


# ------------------------- delete_instance -------------------------


@mock_aws
def test_delete_instance_removes_it() -> None:
    adapter = AwsRdsAdapter()
    created = adapter.create_instance(
        InstanceCreateRequest(name="todelete", admin_password="supersecret123")
    )

    adapter.delete_instance(created.id)

    with pytest.raises(LookupError):
        adapter.get_instance(created.id)


@mock_aws
def test_delete_instance_is_idempotent() -> None:
    adapter = AwsRdsAdapter()
    # Borrar algo que no existe no debe levantar excepción.
    adapter.delete_instance("noexiste-1234")
    adapter.delete_instance("noexiste-5678")
