"""
Modelo abstracto de operaciones sobre datos (agnóstico de motor).

Análogo en el data plane a lo que `app/models.py` es en el control plane:
tipos que NO mencionan ningún motor concreto. `ColumnType.STRING` existe en
este vocabulario; `VARCHAR(255)` o `TEXT` son decisiones de cada dialecto.

Seguridad: los VALORES viajan siempre como parámetros del driver (placeholders
`%s`), nunca interpolados en el SQL. Los IDENTIFICADORES (nombres de tabla y
columna) no se pueden parametrizar en SQL, así que se validan aquí con un
patrón estricto (letras/dígitos/guion bajo) y además cada dialecto los quotea.
Esa doble barrera es la defensa contra inyección de esta capa.
"""

import re
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

# Identificador SQL seguro: letra o guion bajo inicial, resto alfanumérico.
# 63 chars = límite de identificadores de PostgreSQL (MySQL admite 64).
IDENTIFIER_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)

Identifier = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]


class ColumnType(StrEnum):
    """Tipos de columna abstractos. Cada dialecto los mapea a su tipo nativo."""

    INTEGER = "integer"
    BIGINT = "bigint"
    DECIMAL = "decimal"  # exacto, para dinero: NUMERIC/DECIMAL(18,4)
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"  # texto corto indexable: VARCHAR(255)
    TEXT = "text"  # texto largo (no válido como clave primaria en MySQL)
    DATE = "date"
    TIMESTAMP = "timestamp"


class ColumnDef(BaseModel):
    """Definición de una columna en el modelo abstracto."""

    name: Identifier
    type: ColumnType
    primary_key: bool = False
    nullable: bool = True


class TableDefinition(BaseModel):
    """Operación CREATE TABLE en el modelo abstracto."""

    name: Identifier
    columns: list[ColumnDef] = Field(..., min_length=1)

    @field_validator("columns")
    @classmethod
    def _no_duplicate_columns(cls, columns: list[ColumnDef]) -> list[ColumnDef]:
        names = [c.name for c in columns]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Columnas duplicadas: {sorted(duplicates)}")
        return columns


class RowsInsert(BaseModel):
    """Operación INSERT (por lotes) en el modelo abstracto.

    Todas las filas deben tener exactamente las mismas claves: eso permite un
    único INSERT con `executemany`, que es a la vez más simple y más rápido.
    """

    table: Identifier
    rows: list[dict[str, Any]] = Field(..., min_length=1)

    @field_validator("rows")
    @classmethod
    def _validate_rows(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        first_keys = set(rows[0])
        if not first_keys:
            raise ValueError("Cada fila debe tener al menos una columna")
        for key in first_keys:
            if not _IDENTIFIER_RE.match(key):
                raise ValueError(f"Nombre de columna inválido: {key!r}")
        for i, row in enumerate(rows[1:], start=2):
            if set(row) != first_keys:
                raise ValueError(
                    f"La fila {i} tiene columnas distintas de la primera "
                    f"({sorted(set(row))} vs {sorted(first_keys)})"
                )
        return rows

    @property
    def columns(self) -> list[str]:
        """Columnas del INSERT, en orden estable (el de la primera fila)."""
        return list(self.rows[0])


class FilterOp(StrEnum):
    """Operadores de comparación soportados en los filtros del SELECT."""

    EQ = "="
    NEQ = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    LIKE = "like"


class Filter(BaseModel):
    """Condición `columna <op> valor` de un SELECT. El valor va parametrizado."""

    column: Identifier
    op: FilterOp = FilterOp.EQ
    value: Any


class SelectQuery(BaseModel):
    """Operación SELECT en el modelo abstracto.

    Deliberadamente simple: una tabla, columnas opcionales, filtros AND,
    orden y límite. Sin joins ni agregaciones — ampliar el conjunto es
    trabajo futuro (ver CLAUDE.md §10.2).
    """

    table: Identifier
    columns: list[Identifier] | None = None  # None => todas (*)
    filters: list[Filter] = []
    order_by: Identifier | None = None
    descending: bool = False
    limit: int | None = Field(default=100, ge=1, le=1000)
