"""
Tests del adaptador GcpCloudSqlAdapter.

GCP no tiene un equivalente a `moto`, así que inyectamos un mock del cliente
discovery (`GcpCloudSqlAdapter(service=mock)`). Eso permite testear la lógica
del adaptador sin tocar la red ni necesitar credenciales. Se complementa con
tests directos de las funciones puras de mapeo (sin mock alguno).
"""

from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.adapters.gcp_cloudsql import (
    SIZE_TO_TIER,
    GcpCloudSqlAdapter,
    _database_version_to_engine_and_version,
    _gcp_state_to_status,
)
from app.models import (
    DatabaseEngine,
    InstanceCreateRequest,
    InstanceRegion,
    InstanceSize,
    InstanceStatus,
)


@pytest.fixture(autouse=True)
def _fake_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """IP pública falsa: evita la llamada HTTP real de detección (red auto)."""
    monkeypatch.setattr("app.network.get_public_ip", lambda: "203.0.113.5")


def _make_item(
    *,
    instance_id: str = "abc12345",
    name: str = "ventas",
    db_version: str = "POSTGRES_16",
    state: str = "RUNNABLE",
    ip: str | None = "34.1.2.3",
    managed: bool = True,
) -> dict:
    """Construye un item de la API de Cloud SQL como el que devolvería list/get."""
    labels = {"instance-id": instance_id, "instance-name": name}
    if managed:
        labels["managed-by"] = "cloud-db-api"
    item: dict = {
        "name": f"cloudapi-{name}-{instance_id}",
        "databaseVersion": db_version,
        "state": state,
        "settings": {"userLabels": labels},
    }
    if ip:
        item["ipAddresses"] = [{"type": "PRIMARY", "ipAddress": ip}]
    return item


def _adapter_with_items(items: list[dict]) -> tuple[GcpCloudSqlAdapter, MagicMock]:
    """Adaptador con un service mockeado cuyo list() devuelve `items`."""
    service = MagicMock()
    service.instances.return_value.list.return_value.execute.return_value = {"items": items}
    adapter = GcpCloudSqlAdapter(service=service)
    return adapter, service


# ------------------------- funciones puras (sin mock) -------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PENDING_CREATE", InstanceStatus.CREATING),
        ("RUNNABLE", InstanceStatus.AVAILABLE),
        ("SUSPENDED", InstanceStatus.STOPPED),
        ("PENDING_DELETE", InstanceStatus.DELETING),
        ("FAILED", InstanceStatus.ERROR),
        ("ESTADO_DESCONOCIDO", InstanceStatus.CREATING),
    ],
)
def test_gcp_state_mapping(raw: str, expected: InstanceStatus) -> None:
    assert _gcp_state_to_status(raw) == expected


def test_database_version_parsing() -> None:
    assert _database_version_to_engine_and_version("POSTGRES_16") == (
        DatabaseEngine.POSTGRES,
        "16",
    )
    assert _database_version_to_engine_and_version("MYSQL_8_0") == (
        DatabaseEngine.MYSQL,
        "8.0",
    )


# ------------------------- create_instance -------------------------


def test_create_calls_insert_with_correct_body() -> None:
    adapter, service = _adapter_with_items([])
    adapter.create_instance(InstanceCreateRequest(name="ventas", admin_password="supersecret123"))

    insert = service.instances.return_value.insert
    insert.assert_called_once()
    body = insert.call_args.kwargs["body"]
    assert body["databaseVersion"] == "POSTGRES_16"
    assert body["settings"]["tier"] == SIZE_TO_TIER[InstanceSize.SMALL]
    assert body["settings"]["edition"] == "ENTERPRISE"  # tier barato solo aquí
    assert body["rootPassword"] == "supersecret123"
    labels = body["settings"]["userLabels"]
    assert labels["managed-by"] == "cloud-db-api"
    assert labels["instance-name"] == "ventas"


def test_create_returns_creating_status() -> None:
    adapter, _ = _adapter_with_items([])
    info = adapter.create_instance(
        InstanceCreateRequest(name="ventas", admin_password="supersecret123")
    )
    assert info.status == InstanceStatus.CREATING
    assert info.provider == "gcp_cloudsql"


def test_create_authorizes_api_public_ip() -> None:
    """Red automática: la IP pública de la API entra en authorizedNetworks."""
    adapter, service = _adapter_with_items([])
    info = adapter.create_instance(
        InstanceCreateRequest(name="ventas", admin_password="supersecret123")
    )
    body = service.instances.return_value.insert.call_args.kwargs["body"]
    ip_config = body["settings"]["ipConfiguration"]
    assert ip_config["ipv4Enabled"] is True
    assert ip_config["authorizedNetworks"] == [
        {"name": "cloud-db-api-client", "value": "203.0.113.5/32"}
    ]
    assert info.note is None  # red abierta => sin aviso


def test_create_without_public_ip_degrades_with_note(monkeypatch) -> None:
    """Sin IP detectable: se crea igual, red cerrada, y se avisa en note."""
    monkeypatch.setattr("app.network.get_public_ip", lambda: None)
    adapter, service = _adapter_with_items([])
    info = adapter.create_instance(
        InstanceCreateRequest(name="ventas", admin_password="supersecret123")
    )
    body = service.instances.return_value.insert.call_args.kwargs["body"]
    assert "authorizedNetworks" not in body["settings"]["ipConfiguration"]
    assert info.note is not None and "IP pública" in info.note
    assert info.host is None
    assert info.port is None
    assert info.id


def test_create_supports_mysql() -> None:
    adapter, service = _adapter_with_items([])
    adapter.create_instance(
        InstanceCreateRequest(
            name="ventas",
            engine=DatabaseEngine.MYSQL,
            admin_password="supersecret123",
        )
    )
    body = service.instances.return_value.insert.call_args.kwargs["body"]
    assert body["databaseVersion"] == "MYSQL_8_0"


def test_create_large_uses_bigger_tier() -> None:
    adapter, service = _adapter_with_items([])
    adapter.create_instance(
        InstanceCreateRequest(
            name="ventas", size=InstanceSize.LARGE, admin_password="supersecret123"
        )
    )
    body = service.instances.return_value.insert.call_args.kwargs["body"]
    assert body["settings"]["tier"] == "db-n1-standard-1"


def test_create_maps_region_to_native() -> None:
    adapter, service = _adapter_with_items([])
    adapter.create_instance(
        InstanceCreateRequest(
            name="ventas", region=InstanceRegion.US, admin_password="supersecret123"
        )
    )
    body = service.instances.return_value.insert.call_args.kwargs["body"]
    assert body["region"] == "us-east1"


def test_create_with_custom_storage() -> None:
    adapter, service = _adapter_with_items([])
    adapter.create_instance(
        InstanceCreateRequest(name="ventas", storage_gb=25, admin_password="supersecret123")
    )
    body = service.instances.return_value.insert.call_args.kwargs["body"]
    assert body["settings"]["dataDiskSizeGb"] == "25"


# ------------------------- list_instances -------------------------


def test_list_empty_returns_empty_list() -> None:
    adapter, _ = _adapter_with_items([])
    assert adapter.list_instances() == []


def test_list_returns_only_managed_instances() -> None:
    mine = _make_item(instance_id="mine1234", name="mia", managed=True)
    alien = _make_item(instance_id="alien999", name="ajena", managed=False)
    adapter, _ = _adapter_with_items([mine, alien])

    listed = adapter.list_instances()
    assert len(listed) == 1
    assert listed[0].name == "mia"
    assert listed[0].provider == "gcp_cloudsql"


def test_instance_to_info_available_has_host_and_port() -> None:
    item = _make_item(state="RUNNABLE", ip="34.1.2.3")
    adapter, _ = _adapter_with_items([item])
    info = adapter.list_instances()[0]
    assert info.status == InstanceStatus.AVAILABLE
    assert info.host == "34.1.2.3"
    assert info.port == 5432


# ------------------------- get_instance -------------------------


def test_get_instance_found() -> None:
    item = _make_item(instance_id="abc12345", name="ventas")
    adapter, _ = _adapter_with_items([item])
    info = adapter.get_instance("abc12345")
    assert info.id == "abc12345"
    assert info.name == "ventas"


def test_get_instance_not_found_raises() -> None:
    adapter, _ = _adapter_with_items([])
    with pytest.raises(LookupError):
        adapter.get_instance("noexiste")


# ------------------------- delete_instance -------------------------


def test_delete_existing_calls_api() -> None:
    item = _make_item(instance_id="abc12345", name="ventas")
    adapter, service = _adapter_with_items([item])
    adapter.delete_instance("abc12345")
    service.instances.return_value.delete.assert_called_once()


def test_delete_is_idempotent() -> None:
    adapter, service = _adapter_with_items([])
    adapter.delete_instance("noexiste")  # no debe lanzar
    service.instances.return_value.delete.assert_not_called()


# ------------------------- aprovisionamiento diferido (B6+B7) -------------------------


def _adapter_runnable() -> tuple[GcpCloudSqlAdapter, MagicMock]:
    """Adaptador cuyo service responde RUNNABLE al consultar la instancia."""
    service = MagicMock()
    service.instances.return_value.get.return_value.execute.return_value = {"state": "RUNNABLE"}
    return GcpCloudSqlAdapter(service=service), service


def test_post_provision_creates_db_and_user() -> None:
    adapter, service = _adapter_runnable()
    adapter._post_provision("abc123", "cloudapi-ventas-abc123", "ventas", "dbadmin", "pw")
    service.databases.return_value.insert.assert_called_once()
    service.users.return_value.insert.assert_called_once()
    assert adapter._setup_status["abc123"] is None  # todo OK, sin nota


def test_post_provision_reports_user_failure() -> None:
    adapter, service = _adapter_runnable()
    service.users.return_value.insert.return_value.execute.side_effect = Exception("boom")
    adapter._post_provision("abc123", "cloudapi-ventas-abc123", "ventas", "dbadmin", "pw")
    note = adapter._setup_status["abc123"]
    assert note is not None
    assert "usuario" in note.lower()
    assert "dbadmin" in note


def test_post_provision_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = _adapter_runnable()
    monkeypatch.setattr(adapter, "_wait_until_runnable", lambda *a, **k: False)
    adapter._post_provision("abc123", "db", "ventas", "dbadmin", "pw")
    assert "no llegó a estar lista" in adapter._setup_status["abc123"]


def test_wait_until_runnable_timeout_returns_false() -> None:
    service = MagicMock()
    service.instances.return_value.get.return_value.execute.return_value = {
        "state": "PENDING_CREATE"
    }
    adapter = GcpCloudSqlAdapter(service=service)
    assert adapter._wait_until_runnable("db", timeout_s=0) is False


def test_wait_until_runnable_false_si_hay_operaciones_en_curso() -> None:
    """RUNNABLE no basta: con una operación viva, Cloud SQL rechazaría escrituras."""
    service = MagicMock()
    service.instances.return_value.get.return_value.execute.return_value = {"state": "RUNNABLE"}
    service.operations.return_value.list.return_value.execute.return_value = {
        "items": [{"status": "RUNNING", "operationType": "CREATE"}]
    }
    adapter = GcpCloudSqlAdapter(service=service)
    assert adapter._wait_until_runnable("db", timeout_s=0) is False


def test_operations_in_progress_false_cuando_todas_done() -> None:
    service = MagicMock()
    service.operations.return_value.list.return_value.execute.return_value = {
        "items": [{"status": "DONE"}, {"status": "DONE"}]
    }
    adapter = GcpCloudSqlAdapter(service=service)
    assert adapter._operations_in_progress("db") is False


def _http_error(status: int) -> HttpError:
    """HttpError del cliente discovery con el código indicado."""
    return HttpError(resp=httplib2.Response({"status": status}), content=b"{}")


def test_execute_when_idle_reintenta_ante_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """El 409 `operationInProgress` se absorbe reintentando, no propaga."""
    service = MagicMock()
    adapter = GcpCloudSqlAdapter(service=service)
    monkeypatch.setattr("app.adapters.gcp_cloudsql.time.sleep", lambda _: None)

    intentos = {"n": 0}

    def build_request():
        intentos["n"] += 1
        request = MagicMock()
        if intentos["n"] < 3:
            request.execute.side_effect = _http_error(409)
        else:
            request.execute.return_value = {"ok": True}
        return request

    assert adapter._execute_when_idle(build_request, attempts=5, delay_s=0) == {"ok": True}
    assert intentos["n"] == 3


def test_execute_when_idle_propaga_errores_no_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un 403 no es transitorio: debe propagarse en el primer intento."""
    service = MagicMock()
    adapter = GcpCloudSqlAdapter(service=service)
    monkeypatch.setattr("app.adapters.gcp_cloudsql.time.sleep", lambda _: None)

    request = MagicMock()
    request.execute.side_effect = _http_error(403)

    with pytest.raises(HttpError):
        adapter._execute_when_idle(lambda: request, attempts=5, delay_s=0)
    assert request.execute.call_count == 1


def test_note_surfaces_in_get_instance() -> None:
    """El mensaje de setup aparece en el InstanceInfo del polling."""
    item = _make_item(instance_id="abc123", name="ventas")
    adapter, _ = _adapter_with_items([item])
    adapter._setup_status["abc123"] = "Preparando base de datos y usuario..."
    info = adapter.get_instance("abc123")
    assert info.note == "Preparando base de datos y usuario..."
