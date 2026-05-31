"""
Fixtures compartidas por toda la suite de tests.

- `fake_adapter`: instancia limpia de FakeAdapter por test.
- `client`: TestClient de FastAPI con FakeAdapter inyectado en lugar del real.
  Permite testear los endpoints sin necesidad de Docker ni red.
- `docker_adapter`: LocalDockerAdapter real. SOLO para tests de integración.
  Limpia todos los contenedores con el label de la API tras cada test.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.adapters.local_docker import (
    OWNER_LABEL_KEY,
    OWNER_LABEL_VALUE,
    LocalDockerAdapter,
)
from app.main import app, get_adapter
from tests.fakes import FakeAdapter


@pytest.fixture
def fake_adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def client(fake_adapter: FakeAdapter) -> Generator[TestClient, None, None]:
    """TestClient con `get_adapter` sustituido por uno que devuelve el fake.

    `dependency_overrides` es el mecanismo de FastAPI para inyectar
    dependencias falsas en tests. Sin él, los endpoints intentarían crear
    un LocalDockerAdapter y tocarían Docker.
    """
    app.dependency_overrides[get_adapter] = lambda: fake_adapter
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _purge_api_containers(adapter: LocalDockerAdapter) -> None:
    """Borra TODOS los contenedores con el label de la API."""
    for container in adapter.client.containers.list(
        filters={"label": f"{OWNER_LABEL_KEY}={OWNER_LABEL_VALUE}"},
        all=True,
    ):
        container.remove(force=True)


@pytest.fixture
def docker_adapter() -> Generator[LocalDockerAdapter, None, None]:
    """Adaptador real. ATENCIÓN: limpia todos los contenedores cloud-db-api
    antes y después del test. No correr si tienes instancias en uso."""
    adapter = LocalDockerAdapter()
    _purge_api_containers(adapter)  # arranque limpio
    yield adapter
    _purge_api_containers(adapter)  # no dejar basura
