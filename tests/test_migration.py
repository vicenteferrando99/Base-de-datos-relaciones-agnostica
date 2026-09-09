"""
Pruebas de la migración entre instancias (`app/dataops/migration.py`).

Se verifican con dobles de conexión: la lógica interesante ---traducir el
esquema nativo al modelo abstracto y dejar que el dialecto del DESTINO genere
el SQL--- es determinista y no necesita ninguna base de datos real. El copiado
de filas sí se ejercita contra motores reales en las pruebas de integración.
"""

import pytest

from app.dataops import migration
from app.dataops.models import ColumnType
from app.models import DatabaseEngine


class FakeCursor:
    """Cursor DB-API mínimo, guionizado con respuestas por orden de consulta."""

    def __init__(self, responses: list[list[tuple]], recorder: list):
        self._responses = responses
        self._recorder = recorder
        self._current: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._recorder.append(("execute", sql, params))
        self._current = self._responses.pop(0) if self._responses else []

    def executemany(self, sql, params):
        self._recorder.append(("executemany", sql, list(params)))

    def fetchall(self):
        return self._current

    def fetchmany(self, size):
        batch, self._current = self._current[:size], self._current[size:]
        return batch


class FakeConnection:
    def __init__(self, responses: list[list[tuple]] | None = None):
        self.responses = responses or []
        self.statements: list = []

    def cursor(self):
        return FakeCursor(self.responses, self.statements)

    def close(self):
        pass


# --- esquema de origen de ejemplo: una tabla con los cuatro tipos del TFM ---
_COLUMNS = [
    ("prestamos", "id", "integer", "NO"),
    ("prestamos", "cliente", "character varying", "NO"),
    ("prestamos", "importe", "numeric", "YES"),
    ("prestamos", "alta", "date", "YES"),
]
_PKS = [("prestamos", "id")]


def _source(columns=None, pks=None) -> FakeConnection:
    return FakeConnection([list(columns or _COLUMNS), list(pks or _PKS)])


def test_introspect_traduce_tipos_nativos_al_modelo_abstracto():
    report = migration.introspect(_source(), DatabaseEngine.POSTGRES, "ventas")

    assert [t.name for t in report.tables] == ["prestamos"]
    types = {c.name: c.type for c in report.tables[0].columns}
    assert types == {
        "id": ColumnType.INTEGER,
        "cliente": ColumnType.STRING,
        "importe": ColumnType.DECIMAL,
        "alta": ColumnType.DATE,
    }


def test_introspect_marca_clave_primaria_y_nulabilidad():
    report = migration.introspect(_source(), DatabaseEngine.POSTGRES, "ventas")
    columns = {c.name: c for c in report.tables[0].columns}

    assert columns["id"].primary_key is True
    assert columns["id"].nullable is False
    assert columns["importe"].primary_key is False
    assert columns["importe"].nullable is True


def test_introspect_usa_public_en_postgres_y_la_bd_en_mysql():
    source_pg = _source()
    migration.introspect(source_pg, DatabaseEngine.POSTGRES, "ventas")
    assert source_pg.statements[0][2] == ("public",)

    source_my = _source()
    migration.introspect(source_my, DatabaseEngine.MYSQL, "ventas")
    assert source_my.statements[0][2] == ("ventas",)


def test_introspect_reporta_tipos_no_soportados_en_vez_de_inventarlos():
    columns = _COLUMNS + [("prestamos", "extra", "jsonb", "YES")]
    report = migration.introspect(_source(columns), DatabaseEngine.POSTGRES, "ventas")

    assert [c.name for c in report.tables[0].columns] == ["id", "cliente", "importe", "alta"]
    assert len(report.unsupported) == 1
    assert report.unsupported[0].column == "extra"
    assert report.unsupported[0].native_type == "jsonb"


def test_introspect_avisa_de_la_ambiguedad_de_tinyint_en_mysql():
    """MySQL guarda BOOLEAN como TINYINT(1): la migración lo generaliza y avisa."""
    columns = [("t", "flag", "tinyint", "YES")]
    report = migration.introspect(_source(columns, pks=[]), DatabaseEngine.MYSQL, "ventas")

    assert report.tables[0].columns[0].type == ColumnType.INTEGER
    assert any("TINYINT" in w for w in report.warnings)


def test_migracion_entre_motores_genera_el_sql_del_destino():
    """El corazón del asunto: el SQL de creación lo dicta el motor DESTINO."""
    schema = migration.introspect(_source(), DatabaseEngine.POSTGRES, "ventas")
    target = FakeConnection()

    report = migration.migrate(
        source_connection=FakeConnection([[]]),
        target_connection=target,
        source_engine=DatabaseEngine.POSTGRES,
        target_engine=DatabaseEngine.MYSQL,
        schema=schema,
    )

    create_sql = report.tables[0].sql
    assert "`prestamos`" in create_sql  # quoting de MySQL, no el de Postgres
    assert "DECIMAL(18, 4)" in create_sql  # tipo de MySQL, no NUMERIC
    assert '"prestamos"' not in create_sql


def test_migracion_al_mismo_motor_conserva_su_propio_dialecto():
    schema = migration.introspect(_source(), DatabaseEngine.POSTGRES, "ventas")

    report = migration.migrate(
        source_connection=FakeConnection([[]]),
        target_connection=FakeConnection(),
        source_engine=DatabaseEngine.POSTGRES,
        target_engine=DatabaseEngine.POSTGRES,
        schema=schema,
    )

    assert '"prestamos"' in report.tables[0].sql
    assert "NUMERIC(18, 4)" in report.tables[0].sql


def test_migracion_copia_las_filas_por_lotes():
    schema = migration.introspect(_source(), DatabaseEngine.POSTGRES, "ventas")
    rows = [(i, f"cliente{i}", "10.00", "2026-01-01") for i in range(5)]
    target = FakeConnection()

    report = migration.migrate(
        source_connection=FakeConnection([rows]),
        target_connection=target,
        source_engine=DatabaseEngine.POSTGRES,
        target_engine=DatabaseEngine.POSTGRES,
        schema=schema,
        batch_size=2,
    )

    assert report.total_rows == 5
    inserts = [s for s in target.statements if s[0] == "executemany"]
    assert [len(s[2]) for s in inserts] == [2, 2, 1]  # tres lotes


def test_migracion_respeta_la_seleccion_de_tablas():
    columns = _COLUMNS + [("otra", "id", "integer", "NO")]
    schema = migration.introspect(_source(columns), DatabaseEngine.POSTGRES, "ventas")
    assert len(schema.tables) == 2

    report = migration.migrate(
        source_connection=FakeConnection([[], []]),
        target_connection=FakeConnection(),
        source_engine=DatabaseEngine.POSTGRES,
        target_engine=DatabaseEngine.POSTGRES,
        schema=schema,
        tables=["otra"],
    )

    assert [t.table for t in report.tables] == ["otra"]


def test_migracion_sin_crear_tablas_no_emite_create():
    schema = migration.introspect(_source(), DatabaseEngine.POSTGRES, "ventas")
    target = FakeConnection()

    migration.migrate(
        source_connection=FakeConnection([[]]),
        target_connection=target,
        source_engine=DatabaseEngine.POSTGRES,
        target_engine=DatabaseEngine.POSTGRES,
        schema=schema,
        create_tables=False,
    )

    assert not any("CREATE TABLE" in str(s[1]) for s in target.statements)


def test_migracion_arrastra_los_avisos_de_columnas_no_soportadas():
    columns = _COLUMNS + [("prestamos", "extra", "jsonb", "YES")]
    schema = migration.introspect(_source(columns), DatabaseEngine.POSTGRES, "ventas")

    report = migration.migrate(
        source_connection=FakeConnection([[]]),
        target_connection=FakeConnection(),
        source_engine=DatabaseEngine.POSTGRES,
        target_engine=DatabaseEngine.POSTGRES,
        schema=schema,
    )

    assert any("no soportado" in w for w in report.warnings)


def test_existing_tables_consulta_el_esquema_del_destino():
    target = FakeConnection([[("prestamos",), ("clientes",)]])

    present = migration.existing_tables(target, DatabaseEngine.POSTGRES, "ventas")

    assert present == {"prestamos", "clientes"}
    assert target.statements[0][2] == ("public",)


def test_existing_tables_usa_la_bd_como_esquema_en_mysql():
    target = FakeConnection([[("prestamos",)]])

    migration.existing_tables(target, DatabaseEngine.MYSQL, "ventas")

    assert target.statements[0][2] == ("ventas",)


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("bigint", ColumnType.BIGINT),
        ("double precision", ColumnType.FLOAT),
        ("double", ColumnType.FLOAT),
        ("boolean", ColumnType.BOOLEAN),
        ("varchar", ColumnType.STRING),
        ("longtext", ColumnType.TEXT),
        ("datetime", ColumnType.TIMESTAMP),
        ("timestamp without time zone", ColumnType.TIMESTAMP),
    ],
)
def test_mapa_de_tipos_nativos_cubre_ambos_motores(native, expected):
    report = migration.introspect(
        _source([("t", "c", native, "YES")], pks=[]), DatabaseEngine.POSTGRES, "d"
    )
    assert report.tables[0].columns[0].type == expected
