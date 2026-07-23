"""
Tests del data plane: ejecución de SQL (`app/query.py`) y endpoint `/query`.

`run_query` se prueba mockeando el `connect` del driver (sin BD real): psycopg
para Postgres y pymysql para MySQL. El endpoint se prueba con `run_query`
parcheado. Y se cubre `GET /instances?provider=`.
"""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import app.query as q
from app.models import DatabaseEngine


def _col(name: str):
    return type("Col", (), {"name": name})()


def _fake_connection(*, description, fetched, rowcount=0):
    cur = MagicMock()
    cur.description = description
    cur.fetchmany.return_value = fetched
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    connect_cm = MagicMock()
    connect_cm.__enter__.return_value = conn
    return connect_cm, cur


def _patch_connect(monkeypatch, *, description, fetched, rowcount=0, driver="psycopg"):
    connect_cm, cur = _fake_connection(description=description, fetched=fetched, rowcount=rowcount)
    monkeypatch.setattr(getattr(q, driver), "connect", lambda **kwargs: connect_cm)
    return cur


# ------------------------- run_query -------------------------


def test_run_query_select_returns_columns_and_rows(monkeypatch) -> None:
    _patch_connect(
        monkeypatch, description=[_col("id"), _col("nombre")], fetched=[(1, "Ana"), (2, "Bo")]
    )
    res = q.run_query(host="h", port=5432, user="u", password="p", dbname="d", sql="select")
    assert res["columns"] == ["id", "nombre"]
    assert res["rows"] == [[1, "Ana"], [2, "Bo"]]
    assert res["truncated"] is False


def test_run_query_non_select_returns_message(monkeypatch) -> None:
    _patch_connect(monkeypatch, description=None, fetched=[], rowcount=3)
    res = q.run_query(host="h", port=5432, user="u", password="p", dbname="d", sql="insert")
    assert res["columns"] == []
    assert res["rowcount"] == 3
    assert res["message"] == "OK"


def test_run_query_serializes_non_json_types(monkeypatch) -> None:
    _patch_connect(
        monkeypatch,
        description=[_col("importe"), _col("alta")],
        fetched=[(Decimal("10.50"), datetime.date(2026, 1, 1))],
    )
    res = q.run_query(host="h", port=5432, user="u", password="p", dbname="d", sql="select")
    assert res["rows"] == [["10.50", "2026-01-01"]]


def test_run_query_truncates_over_max_rows(monkeypatch) -> None:
    fetched = [(i,) for i in range(q.MAX_ROWS + 1)]
    _patch_connect(monkeypatch, description=[_col("n")], fetched=fetched)
    res = q.run_query(host="h", port=5432, user="u", password="p", dbname="d", sql="select")
    assert res["truncated"] is True
    assert res["rowcount"] == q.MAX_ROWS


def test_run_query_mysql_uses_pymysql_driver(monkeypatch) -> None:
    """Con engine=mysql debe conectar pymysql, no psycopg."""
    # description de pymysql: tuplas DB-API clásicas (el nombre es el índice 0)
    _patch_connect(monkeypatch, description=[("id", None)], fetched=[(7,)], driver="pymysql")

    def psycopg_forbidden(**kwargs):
        raise AssertionError("psycopg.connect no debería usarse con engine=mysql")

    monkeypatch.setattr(q.psycopg, "connect", psycopg_forbidden)
    res = q.run_query(
        engine=DatabaseEngine.MYSQL,
        host="h",
        port=3306,
        user="u",
        password="p",
        dbname="d",
        sql="select",
    )
    assert res["columns"] == ["id"]
    assert res["rows"] == [[7]]


def test_run_query_executemany_for_batch_params(monkeypatch) -> None:
    cur = _patch_connect(monkeypatch, description=None, fetched=[], rowcount=2)
    q.run_query(
        host="h",
        port=5432,
        user="u",
        password="p",
        dbname="d",
        sql="insert",
        params=[(1,), (2,)],
        many=True,
    )
    cur.executemany.assert_called_once_with("insert", [(1,), (2,)])


# ------------------------- endpoint /query -------------------------


def test_query_endpoint_returns_result(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        q, "run_query", lambda **kwargs: {"columns": ["id"], "rows": [[1]], "rowcount": 1}
    )
    resp = client.post(
        "/query",
        json={
            "host": "h",
            "port": 5432,
            "user": "u",
            "password": "p",
            "dbname": "d",
            "sql": "select 1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["columns"] == ["id"]


def test_query_endpoint_maps_errors_to_400(client: TestClient, monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(q, "run_query", boom)
    resp = client.post(
        "/query",
        json={"host": "h", "port": 5432, "user": "u", "password": "p", "dbname": "d", "sql": "x"},
    )
    assert resp.status_code == 400
    assert "connection refused" in resp.json()["detail"]


# ------------------------- GET /instances?provider= -------------------------


def test_list_with_invalid_provider_returns_400(client: TestClient) -> None:
    resp = client.get("/instances", params={"provider": "noexiste"})
    assert resp.status_code == 400
