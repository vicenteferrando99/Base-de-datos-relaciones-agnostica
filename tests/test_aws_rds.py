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
from app.models import (
    DatabaseEngine,
    InstanceCreateRequest,
    InstanceRegion,
    InstanceStatus,
)


@pytest.fixture(autouse=True)
def _aws_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credenciales dummy para que boto3 no busque las reales del sistema."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")


@pytest.fixture(autouse=True)
def _fake_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """IP pública falsa: evita la llamada HTTP real de detección (red auto)."""
    monkeypatch.setattr("app.network.get_public_ip", lambda: "203.0.113.5")


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
    assert db["MasterUsername"] == "dbadmin"
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
    assert info.engine_version == "16.14"


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


# ------------------------- region / storage / B7 -------------------------


@mock_aws
def test_create_sets_initial_dbname_sanitized() -> None:
    """B7: RDS crea una BD con el nombre pedido (guiones saneados)."""
    adapter = AwsRdsAdapter()
    adapter.create_instance(InstanceCreateRequest(name="mi-base", admin_password="supersecret123"))
    client = boto3.client("rds", region_name="eu-west-1")
    db = client.describe_db_instances()["DBInstances"][0]
    assert db["DBName"] == "mi_base"


@mock_aws
def test_create_with_custom_storage() -> None:
    adapter = AwsRdsAdapter()
    adapter.create_instance(
        InstanceCreateRequest(name="ventas", storage_gb=50, admin_password="supersecret123")
    )
    client = boto3.client("rds", region_name="eu-west-1")
    db = client.describe_db_instances()["DBInstances"][0]
    assert db["AllocatedStorage"] == 50


@mock_aws
def test_create_in_us_region_and_multiregion_list_finds_it() -> None:
    """region=US crea en us-east-1; list (multi-región) la encuentra."""
    adapter = AwsRdsAdapter()
    created = adapter.create_instance(
        InstanceCreateRequest(
            name="ventas", region=InstanceRegion.US, admin_password="supersecret123"
        )
    )
    # No está en eu-west-1...
    eu = boto3.client("rds", region_name="eu-west-1")
    assert eu.describe_db_instances()["DBInstances"] == []
    # ...sí en us-east-1...
    us = boto3.client("rds", region_name="us-east-1")
    assert len(us.describe_db_instances()["DBInstances"]) == 1
    # ...y el list multi-región del adaptador la encuentra.
    assert any(i.id == created.id for i in adapter.list_instances())


# ------------------------- red automática (Security Group) -------------------------


@mock_aws
def test_create_opens_network_via_security_group() -> None:
    """create debe: crear el SG, autorizar el puerto desde la IP de la API,
    asociarlo a la instancia y marcarla PubliclyAccessible."""
    adapter = AwsRdsAdapter()
    info = adapter.create_instance(
        InstanceCreateRequest(name="testdb", admin_password="supersecret123")
    )
    assert info.note is None  # red abierta => sin aviso

    ec2 = boto3.client("ec2", region_name="eu-west-1")
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["cloudapi-dbaccess"]}]
    )["SecurityGroups"]
    assert len(sgs) == 1
    rule = sgs[0]["IpPermissions"][0]
    assert (rule["FromPort"], rule["ToPort"]) == (5432, 5432)
    assert rule["IpRanges"][0]["CidrIp"] == "203.0.113.5/32"

    rds = boto3.client("rds", region_name="eu-west-1")
    db = rds.describe_db_instances()["DBInstances"][0]
    assert db["PubliclyAccessible"] is True
    assert db["VpcSecurityGroups"][0]["VpcSecurityGroupId"] == sgs[0]["GroupId"]


@mock_aws
def test_second_create_reuses_security_group() -> None:
    """El SG es compartido: dos creates no deben duplicarlo ni fallar por
    regla repetida (InvalidPermission.Duplicate se ignora)."""
    adapter = AwsRdsAdapter()
    adapter.create_instance(InstanceCreateRequest(name="uno", admin_password="supersecret123"))
    adapter.create_instance(InstanceCreateRequest(name="dos", admin_password="supersecret123"))

    ec2 = boto3.client("ec2", region_name="eu-west-1")
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["cloudapi-dbaccess"]}]
    )["SecurityGroups"]
    assert len(sgs) == 1


@mock_aws
def test_mysql_opens_its_own_port() -> None:
    adapter = AwsRdsAdapter()
    adapter.create_instance(
        InstanceCreateRequest(
            name="mysqldb", engine=DatabaseEngine.MYSQL, admin_password="supersecret123"
        )
    )
    ec2 = boto3.client("ec2", region_name="eu-west-1")
    sg = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["cloudapi-dbaccess"]}]
    )["SecurityGroups"][0]
    ports = {(r["FromPort"], r["ToPort"]) for r in sg["IpPermissions"]}
    assert (3306, 3306) in ports


@mock_aws
def test_create_without_public_ip_degrades_with_note(monkeypatch) -> None:
    """Sin IP detectable: la instancia se crea igual, sin SG propio, con aviso."""
    monkeypatch.setattr("app.network.get_public_ip", lambda: None)
    adapter = AwsRdsAdapter()
    info = adapter.create_instance(
        InstanceCreateRequest(name="testdb", admin_password="supersecret123")
    )
    assert info.note is not None and "IP pública" in info.note

    ec2 = boto3.client("ec2", region_name="eu-west-1")
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["cloudapi-dbaccess"]}]
    )["SecurityGroups"]
    assert sgs == []  # no se creó SG propio


@mock_aws
def test_configured_security_group_is_used_untouched(monkeypatch) -> None:
    """Con AWS_SECURITY_GROUP_ID configurado se usa ese SG y no se crea el propio."""
    ec2 = boto3.client("ec2", region_name="eu-west-1")
    vpc_id = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    custom = ec2.create_security_group(
        GroupName="mi-sg-propio", Description="sg del usuario", VpcId=vpc_id
    )["GroupId"]
    from app.config import settings

    monkeypatch.setattr(settings, "aws_security_group_id", custom)

    adapter = AwsRdsAdapter()
    adapter.create_instance(InstanceCreateRequest(name="testdb", admin_password="supersecret123"))

    rds = boto3.client("rds", region_name="eu-west-1")
    db = rds.describe_db_instances()["DBInstances"][0]
    assert db["VpcSecurityGroups"][0]["VpcSecurityGroupId"] == custom
    own = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["cloudapi-dbaccess"]}]
    )["SecurityGroups"]
    assert own == []
