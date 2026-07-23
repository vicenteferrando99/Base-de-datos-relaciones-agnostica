"""
Tests de la capa de operaciones agnóstica de motor (`app/dataops/`).

Los dialectos son funciones puras (modelo abstracto -> SQL), así que se
prueban SIN base de datos: entra una operación, sale el SQL esperado. La
comparación entre el SQL de Postgres y el de MySQL para la MISMA operación
es, además, la evidencia del agnosticismo de motor (citar en la memoria).

Los endpoints se prueban con `app.query.run_query` parcheado (sin red).
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.query as q
from app.dataops.dialects import MySqlDialect, PostgresDialect, get_dialect
from app.dataops.models import (
    ColumnDef,
    ColumnType,
    Filter,
    FilterOp,
    RowsInsert,
    SelectQuery,
    TableDefinition,
)
from app.models import DatabaseEngine

PG = PostgresDialect()
MY = MySqlDialect()

LOANS = TableDefinition(
    name="prestamos",
    columns=[
        ColumnDef(name="id", type=ColumnType.INTEGER, primary_key=True, nullable=False),
        ColumnDef(name="titular", type=ColumnType.STRING, nullable=False),
        ColumnDef(name="importe", type=ColumnType.DECIMAL),
        ColumnDef(name="alta", type=ColumnType.TIMESTAMP),
    ],
)


# ------------------------- dialectos: CREATE TABLE -------------------------


def test_create_table_postgres() -> None:
    sql = PG.create_table(LOANS)
    assert sql == (
        'CREATE TABLE "prestamos" (\n'
        '  "id" INTEGER NOT NULL,\n'
        '  "titular" VARCHAR(255) NOT NULL,\n'
        '  "importe" NUMERIC(18, 4),\n'
        '  "alta" TIMESTAMP,\n'
        '  PRIMARY KEY ("id")\n'
        ")"
    )


def test_create_table_mysql() -> None:
    sql = MY.create_table(LOANS)
    assert sql == (
        "CREATE TABLE `prestamos` (\n"
        "  `id` INTEGER NOT NULL,\n"
        "  `titular` VARCHAR(255) NOT NULL,\n"
        "  `importe` DECIMAL(18, 4),\n"
        "  `alta` DATETIME,\n"
        "  PRIMARY KEY (`id`)\n"
        ")"
    )


def test_create_table_composite_primary_key() -> None:
    table = TableDefinition(
        name="t",
        columns=[
            ColumnDef(name="a", type=ColumnType.INTEGER, primary_key=True),
            ColumnDef(name="b", type=ColumnType.INTEGER, primary_key=True),
        ],
    )
    assert 'PRIMARY KEY ("a", "b")' in PG.create_table(table)


# ------------------------- dialectos: INSERT -------------------------


def test_insert_generates_placeholders_and_param_rows() -> None:
    ins = RowsInsert(
        table="prestamos",
        rows=[
            {"id": 1, "titular": "Ana", "importe": 1000},
            {"id": 2, "titular": "Bo", "importe": 2000},
        ],
    )
    sql, params = PG.insert(ins)
    assert sql == 'INSERT INTO "prestamos" ("id", "titular", "importe") VALUES (%s, %s, %s)'
    assert params == [(1, "Ana", 1000), (2, "Bo", 2000)]


def test_insert_mysql_uses_backticks() -> None:
    ins = RowsInsert(table="t", rows=[{"x": 1}])
    sql, _ = MY.insert(ins)
    assert sql == "INSERT INTO `t` (`x`) VALUES (%s)"


# ------------------------- dialectos: SELECT -------------------------


def test_select_defaults_to_star_with_limit() -> None:
    sql, params = PG.select(SelectQuery(table="prestamos"))
    assert sql == 'SELECT * FROM "prestamos" LIMIT 100'
    assert params == ()


def test_select_with_filters_order_and_limit() -> None:
    query = SelectQuery(
        table="prestamos",
        columns=["id", "importe"],
        filters=[
            Filter(column="importe", op=FilterOp.GTE, value=1000),
            Filter(column="titular", op=FilterOp.LIKE, value="A%"),
        ],
        order_by="importe",
        descending=True,
        limit=10,
    )
    sql, params = PG.select(query)
    assert sql == (
        'SELECT "id", "importe" FROM "prestamos" '
        'WHERE "importe" >= %s AND "titular" LIKE %s '
        'ORDER BY "importe" DESC LIMIT 10'
    )
    assert params == (1000, "A%")


def test_select_without_limit() -> None:
    sql, _ = PG.select(SelectQuery(table="t", limit=None))
    assert sql == 'SELECT * FROM "t"'


def test_get_dialect_covers_all_engines() -> None:
    for engine in DatabaseEngine:
        assert get_dialect(engine) is not None


# ------------------------- validación (anti-inyección) -------------------------


def test_table_name_with_injection_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TableDefinition(
            name="users; DROP TABLE users;--",
            columns=[ColumnDef(name="id", type=ColumnType.INTEGER)],
        )


def test_duplicate_columns_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TableDefinition(
            name="t",
            columns=[
                ColumnDef(name="id", type=ColumnType.INTEGER),
                ColumnDef(name="id", type=ColumnType.TEXT),
            ],
        )


def test_insert_rejects_invalid_column_key() -> None:
    with pytest.raises(ValidationError):
        RowsInsert(table="t", rows=[{"bad name": 1}])


def test_insert_rejects_mismatched_row_keys() -> None:
    with pytest.raises(ValidationError):
        RowsInsert(table="t", rows=[{"a": 1}, {"b": 2}])


def test_filter_column_with_injection_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SelectQuery(table="t", filters=[Filter(column="x=1;--", value=1)])


# ------------------------- endpoints /data/* -------------------------


CONN = {
    "engine": "postgres",
    "host": "h",
    "port": 5432,
    "user": "u",
    "password": "p",
    "dbname": "d",
}


@pytest.fixture
def run_query_spy(monkeypatch):
    """Parchea `run_query` y captura los kwargs con los que se llamó."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"columns": [], "rows": [], "rowcount": 1, "message": "OK"}

    monkeypatch.setattr(q, "run_query", fake)
    return calls


def test_create_table_endpoint_returns_generated_sql(
    client: TestClient, run_query_spy: list
) -> None:
    resp = client.post(
        "/data/tables",
        json={
            "connection": CONN,
            "table": {"name": "t", "columns": [{"name": "id", "type": "integer"}]},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["sql"].startswith('CREATE TABLE "t"')
    assert run_query_spy[0]["engine"] == DatabaseEngine.POSTGRES


def test_create_table_endpoint_uses_mysql_dialect(client: TestClient, run_query_spy: list) -> None:
    resp = client.post(
        "/data/tables",
        json={
            "connection": {**CONN, "engine": "mysql"},
            "table": {"name": "t", "columns": [{"name": "id", "type": "integer"}]},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["sql"].startswith("CREATE TABLE `t`")
    assert run_query_spy[0]["engine"] == DatabaseEngine.MYSQL


def test_insert_endpoint_executes_many(client: TestClient, run_query_spy: list) -> None:
    resp = client.post(
        "/data/rows",
        json={
            "connection": CONN,
            "insert": {"table": "t", "rows": [{"x": 1}, {"x": 2}]},
        },
    )
    assert resp.status_code == 201
    assert run_query_spy[0]["many"] is True
    assert run_query_spy[0]["params"] == [(1,), (2,)]


def test_select_endpoint_returns_rows(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        q,
        "run_query",
        lambda **kw: {"columns": ["x"], "rows": [[1]], "rowcount": 1, "truncated": False},
    )
    resp = client.post(
        "/data/select",
        json={"connection": CONN, "query": {"table": "t"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == [[1]]
    assert body["sql"] == 'SELECT * FROM "t" LIMIT 100'


def test_data_endpoint_maps_driver_errors_to_400(client: TestClient, monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(q, "run_query", boom)
    resp = client.post(
        "/data/select",
        json={"connection": CONN, "query": {"table": "t"}},
    )
    assert resp.status_code == 400
    assert "connection refused" in resp.json()["detail"]


def test_data_endpoint_rejects_invalid_table_name(client: TestClient) -> None:
    resp = client.post(
        "/data/select",
        json={"connection": CONN, "query": {"table": "t; DROP TABLE t"}},
    )
    assert resp.status_code == 422  # lo corta la validación Pydantic, ni llega al SQL
