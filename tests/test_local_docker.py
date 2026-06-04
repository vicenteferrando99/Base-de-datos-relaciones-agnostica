"""
Tests del LocalDockerAdapter.

Dos capas:
- Unitarios (rápidos, no necesitan Docker): mapeo de estados, construcción
  de variables de entorno, traducción contenedor->InstanceInfo con mock.
- Integración (lentos, marcados `@pytest.mark.integration`): crean y borran
  contenedores Postgres reales. Requieren el daemon de Docker accesible.

Para correr solo los rápidos:        uv run pytest -m "not integration"
Para correr solo los de integración: uv run pytest -m integration
"""

from types import SimpleNamespace

import pytest

from app.adapters.local_docker import (
    LocalDockerAdapter,
    _docker_status_to_instance_status,
)
from app.models import DatabaseEngine, InstanceCreateRequest, InstanceStatus

# --------------------------------------------------------------------------
# Capa 2: unitarios (rápidos, no tocan Docker)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("running", InstanceStatus.AVAILABLE),
        ("created", InstanceStatus.CREATING),
        ("restarting", InstanceStatus.CREATING),
        ("paused", InstanceStatus.STOPPED),
        ("exited", InstanceStatus.STOPPED),
        ("dead", InstanceStatus.ERROR),
        ("removing", InstanceStatus.DELETING),
        ("estado-marciano-que-no-existe", InstanceStatus.ERROR),
    ],
)
def test_docker_status_mapping(raw: str, expected: InstanceStatus) -> None:
    assert _docker_status_to_instance_status(raw) == expected


def test_build_env_postgres() -> None:
    req = InstanceCreateRequest(
        name="midb",
        engine=DatabaseEngine.POSTGRES,
        admin_username="alice",
        admin_password="supersecret123",
    )
    env = LocalDockerAdapter._build_env(req)
    assert env == {
        "POSTGRES_USER": "alice",
        "POSTGRES_PASSWORD": "supersecret123",
        "POSTGRES_DB": "midb",
    }


def test_build_env_mysql() -> None:
    req = InstanceCreateRequest(
        name="midb",
        engine=DatabaseEngine.MYSQL,
        admin_username="alice",
        admin_password="supersecret123",
    )
    env = LocalDockerAdapter._build_env(req)
    assert env == {
        "MYSQL_ROOT_PASSWORD": "supersecret123",
        "MYSQL_DATABASE": "midb",
        "MYSQL_USER": "alice",
        "MYSQL_PASSWORD": "supersecret123",
    }


def test_container_to_info_running() -> None:
    """Reconstrucción de InstanceInfo a partir de un container running."""
    fake_container = SimpleNamespace(
        labels={
            "managed-by": "cloud-db-api",
            "instance-id": "abcd1234",
            "instance-name": "midb",
            "engine": "postgres",
            "engine-version": "16",
        },
        ports={"5432/tcp": [{"HostPort": "55432"}]},
        status="running",
        name="cloudapi-midb-abcd1234",
    )
    info = LocalDockerAdapter._container_to_info(fake_container)
    assert info.id == "abcd1234"
    assert info.name == "midb"
    assert info.engine == DatabaseEngine.POSTGRES
    assert info.engine_version == "16"
    assert info.status == InstanceStatus.AVAILABLE
    assert info.host == "localhost"
    assert info.port == 55432
    assert info.provider == "local_docker"


def test_container_to_info_stopped_without_ports() -> None:
    """Contenedor parado => sin puerto mapeado y host = None."""
    fake_container = SimpleNamespace(
        labels={
            "managed-by": "cloud-db-api",
            "instance-id": "abcd1234",
            "instance-name": "midb",
            "engine": "postgres",
            "engine-version": "16",
        },
        ports={},
        status="exited",
        name="cloudapi-midb-abcd1234",
    )
    info = LocalDockerAdapter._container_to_info(fake_container)
    assert info.status == InstanceStatus.STOPPED
    assert info.host is None
    assert info.port is None


# --------------------------------------------------------------------------
# Capa 3: integración (lentos, requieren Docker corriendo)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_integration_full_lifecycle(docker_adapter: LocalDockerAdapter) -> None:
    """Flujo completo: create -> get -> list -> delete -> get(404)."""
    req = InstanceCreateRequest(name="ittest", admin_password="supersecret123")

    created = docker_adapter.create_instance(req)
    assert created.engine == DatabaseEngine.POSTGRES
    assert created.host == "localhost"
    assert created.port is not None
    assert created.provider == "local_docker"

    fetched = docker_adapter.get_instance(created.id)
    assert fetched.id == created.id
    assert fetched.name == "ittest"

    listed = docker_adapter.list_instances()
    assert any(i.id == created.id for i in listed)

    docker_adapter.delete_instance(created.id)

    with pytest.raises(LookupError):
        docker_adapter.get_instance(created.id)


@pytest.mark.integration
def test_integration_delete_is_idempotent(
    docker_adapter: LocalDockerAdapter,
) -> None:
    """Borrar instancias que no existen no debe lanzar excepción."""
    docker_adapter.delete_instance("noexiste-1234")
    docker_adapter.delete_instance("noexiste-5678")
    # Si llegamos aquí sin excepción, la idempotencia funciona.
