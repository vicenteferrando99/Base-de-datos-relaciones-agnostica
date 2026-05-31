"""
Tests de los endpoints HTTP, usando FakeAdapter inyectado por dependencia.

NO necesitan Docker: van rápido y son deterministas. Verifican:
- Códigos de estado (201, 200, 204, 404, 422).
- Estructura del JSON de respuesta.
- Comportamiento idempotente del DELETE.
- Validaciones de Pydantic en el body del POST.
"""

from fastapi.testclient import TestClient

VALID_BODY = {"name": "midb", "admin_password": "supersecret123"}


def test_create_returns_201_and_full_info(client: TestClient) -> None:
    response = client.post("/instances", json=VALID_BODY)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "midb"
    assert body["engine"] == "postgres"
    assert body["provider"] == "fake"
    assert body["status"] == "available"
    assert body["id"]  # id no vacío


def test_list_empty_returns_empty_array(client: TestClient) -> None:
    response = client.get("/instances")
    assert response.status_code == 200
    assert response.json() == []


def test_create_then_list_returns_one(client: TestClient) -> None:
    client.post("/instances", json=VALID_BODY)
    response = client.get("/instances")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "midb"


def test_get_existing_instance_returns_same_payload(client: TestClient) -> None:
    created = client.post("/instances", json=VALID_BODY).json()
    response = client.get(f"/instances/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_inexistent_returns_404(client: TestClient) -> None:
    response = client.get("/instances/noexiste")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_delete_existing_returns_204(client: TestClient) -> None:
    created = client.post("/instances", json=VALID_BODY).json()
    response = client.delete(f"/instances/{created['id']}")
    assert response.status_code == 204
    assert response.content == b""  # 204 nunca lleva body


def test_delete_inexistent_also_returns_204(client: TestClient) -> None:
    """Idempotencia: el adaptador no diferencia exista o no."""
    response = client.delete("/instances/noexiste")
    assert response.status_code == 204


def test_delete_twice_is_safe(client: TestClient) -> None:
    created = client.post("/instances", json=VALID_BODY).json()
    first = client.delete(f"/instances/{created['id']}")
    second = client.delete(f"/instances/{created['id']}")
    assert first.status_code == 204
    assert second.status_code == 204


def test_create_rejects_short_password_with_422(client: TestClient) -> None:
    response = client.post(
        "/instances", json={"name": "midb", "admin_password": "short"}
    )
    assert response.status_code == 422


def test_create_rejects_short_name_with_422(client: TestClient) -> None:
    response = client.post(
        "/instances", json={"name": "ab", "admin_password": "supersecret123"}
    )
    assert response.status_code == 422


def test_create_rejects_missing_required_fields_with_422(
    client: TestClient,
) -> None:
    response = client.post("/instances", json={"name": "midb"})  # falta password
    assert response.status_code == 422
