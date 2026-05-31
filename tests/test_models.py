"""Tests de validación de los modelos Pydantic."""

import pytest
from pydantic import ValidationError

from app.models import (
    DatabaseEngine,
    InstanceCreateRequest,
    InstanceInfo,
    InstanceSize,
    InstanceStatus,
)


def test_create_request_valid_minimal() -> None:
    req = InstanceCreateRequest(name="testdb", admin_password="supersecret123")
    assert req.engine == DatabaseEngine.POSTGRES
    assert req.size == InstanceSize.SMALL
    assert req.admin_username == "admin"


def test_create_request_rejects_short_password() -> None:
    with pytest.raises(ValidationError):
        InstanceCreateRequest(name="testdb", admin_password="short")


def test_create_request_rejects_short_name() -> None:
    with pytest.raises(ValidationError):
        InstanceCreateRequest(name="ab", admin_password="supersecret123")


def test_create_request_accepts_mysql() -> None:
    req = InstanceCreateRequest(
        name="testdb",
        engine=DatabaseEngine.MYSQL,
        admin_password="supersecret123",
    )
    assert req.engine == DatabaseEngine.MYSQL


def test_create_request_rejects_unknown_engine() -> None:
    with pytest.raises(ValidationError):
        InstanceCreateRequest(
            name="testdb", engine="oracle", admin_password="supersecret123"
        )


def test_instance_info_allows_optional_host_and_port() -> None:
    info = InstanceInfo(
        id="abc12345",
        name="testdb",
        engine=DatabaseEngine.POSTGRES,
        engine_version="16",
        status=InstanceStatus.STOPPED,
        provider="local_docker",
    )
    assert info.host is None
    assert info.port is None
