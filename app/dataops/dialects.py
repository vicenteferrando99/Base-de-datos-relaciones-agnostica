"""
Dialectos SQL: generación de SQL nativo a partir del modelo abstracto.

Espejo en el data plane del patrón adaptador del control plane:

- `SqlDialect` es el contrato (como `DatabaseAdapter`).
- `PostgresDialect` / `MySqlDialect` son las implementaciones.
- `get_dialect()` es el registry (un dict basta: los dialectos no tienen
  estado ni configuración, no hace falta el decorador de `adapters/registry`).

Cada método devuelve el SQL y, cuando hay valores, los parámetros aparte:
los valores NUNCA se interpolan en la cadena (van como placeholders `%s`,
que es el estilo de psycopg y PyMySQL). Los identificadores llegan ya
validados por el modelo (`app/dataops/models.py`) y aquí además se quotean.

Las diferencias reales entre motores quedan a la vista en los TYPE_MAP y el
quoting — material directo para la memoria: esto es exactamente lo que un
cliente tendría que conocer de cada motor y ya no necesita conocer.
"""

from abc import ABC, abstractmethod

from app.dataops.models import (
    ColumnType,
    FilterOp,
    RowsInsert,
    SelectQuery,
    TableDefinition,
)
from app.models import DatabaseEngine


class SqlDialect(ABC):
    """Contrato: traducir operaciones abstractas a SQL de un motor concreto."""

    #: Mapeo tipo abstracto -> tipo nativo. Lo define cada dialecto.
    TYPE_MAP: dict[ColumnType, str]

    @abstractmethod
    def quote(self, identifier: str) -> str:
        """Quotea un identificador (tabla/columna) según el motor."""
        ...

    # Las tres operaciones comparten estructura entre motores; solo cambian
    # quoting y tipos. Por eso viven aquí como métodos concretos y no en cada
    # dialecto: menos duplicación y la asimetría queda concentrada.

    def create_table(self, table: TableDefinition) -> str:
        """CREATE TABLE. Devuelve solo SQL (no hay valores que parametrizar)."""
        column_lines = []
        for col in table.columns:
            parts = [self.quote(col.name), self.TYPE_MAP[col.type]]
            if not col.nullable:
                parts.append("NOT NULL")
            column_lines.append(" ".join(parts))
        pk = [c.name for c in table.columns if c.primary_key]
        if pk:
            column_lines.append(f"PRIMARY KEY ({', '.join(self.quote(c) for c in pk)})")
        return f"CREATE TABLE {self.quote(table.name)} (\n  " + ",\n  ".join(column_lines) + "\n)"

    def insert(self, ins: RowsInsert) -> tuple[str, list[tuple]]:
        """INSERT por lotes. Devuelve (sql, filas de parámetros) para executemany."""
        cols = ins.columns
        placeholders = ", ".join(["%s"] * len(cols))
        sql = (
            f"INSERT INTO {self.quote(ins.table)} "
            f"({', '.join(self.quote(c) for c in cols)}) "
            f"VALUES ({placeholders})"
        )
        params = [tuple(row[c] for c in cols) for row in ins.rows]
        return sql, params

    def select(self, query: SelectQuery) -> tuple[str, tuple]:
        """SELECT con filtros AND, orden y límite. Devuelve (sql, parámetros)."""
        cols = "*" if not query.columns else ", ".join(self.quote(c) for c in query.columns)
        sql = f"SELECT {cols} FROM {self.quote(query.table)}"
        params: list = []
        if query.filters:
            conditions = []
            for f in query.filters:
                operator = "LIKE" if f.op == FilterOp.LIKE else f.op.value
                conditions.append(f"{self.quote(f.column)} {operator} %s")
                params.append(f.value)
            sql += " WHERE " + " AND ".join(conditions)
        if query.order_by:
            sql += f" ORDER BY {self.quote(query.order_by)}"
            if query.descending:
                sql += " DESC"
        if query.limit is not None:
            sql += f" LIMIT {query.limit}"  # validado como int 1..1000, no inyectable
        return sql, tuple(params)


class PostgresDialect(SqlDialect):
    """SQL de PostgreSQL: identificadores con comillas dobles."""

    TYPE_MAP = {
        ColumnType.INTEGER: "INTEGER",
        ColumnType.BIGINT: "BIGINT",
        ColumnType.DECIMAL: "NUMERIC(18, 4)",
        ColumnType.FLOAT: "DOUBLE PRECISION",
        ColumnType.BOOLEAN: "BOOLEAN",
        ColumnType.STRING: "VARCHAR(255)",
        ColumnType.TEXT: "TEXT",
        ColumnType.DATE: "DATE",
        ColumnType.TIMESTAMP: "TIMESTAMP",
    }

    def quote(self, identifier: str) -> str:
        return f'"{identifier}"'


class MySqlDialect(SqlDialect):
    """SQL de MySQL: backticks, DECIMAL, DOUBLE y DATETIME.

    `TIMESTAMP` existe en MySQL pero con rango limitado (1970-2038) y
    semántica de zona horaria distinta; `DATETIME` es el equivalente sano
    del TIMESTAMP de Postgres.
    """

    TYPE_MAP = {
        ColumnType.INTEGER: "INTEGER",
        ColumnType.BIGINT: "BIGINT",
        ColumnType.DECIMAL: "DECIMAL(18, 4)",
        ColumnType.FLOAT: "DOUBLE",
        ColumnType.BOOLEAN: "BOOLEAN",
        ColumnType.STRING: "VARCHAR(255)",
        ColumnType.TEXT: "TEXT",
        ColumnType.DATE: "DATE",
        ColumnType.TIMESTAMP: "DATETIME",
    }

    def quote(self, identifier: str) -> str:
        return f"`{identifier}`"


_DIALECTS: dict[DatabaseEngine, SqlDialect] = {
    DatabaseEngine.POSTGRES: PostgresDialect(),
    DatabaseEngine.MYSQL: MySqlDialect(),
}


def get_dialect(engine: DatabaseEngine) -> SqlDialect:
    """Dialecto para un motor. KeyError imposible: el enum acota los valores."""
    return _DIALECTS[engine]
