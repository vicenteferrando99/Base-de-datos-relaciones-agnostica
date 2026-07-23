"""
Ejecución de SQL contra una instancia (DATA PLANE).

Este módulo cruza deliberadamente la frontera control plane / data plane: la
API, además de aprovisionar instancias, permite ejecutar SQL contra ellas
(pestaña "Consultas" de la UI). Se conecta con el driver adecuado al motor
(psycopg para PostgreSQL, PyMySQL para MySQL) usando datos de conexión que
aporta el cliente en cada petición (host, puerto, usuario, contraseña); la
API NO almacena credenciales.

Ambos drivers implementan DB-API 2.0 con placeholders `%s`, así que el resto
del módulo es idéntico para los dos motores: elegir driver es un lookup en
`_CONNECTORS`. Misma idea que el registry de adaptadores del control plane,
en miniatura.

⚠️ AVISO DE SEGURIDAD: `run_query` con SQL libre ejecuta SQL arbitrario. Es
una herramienta de demostración para entorno LOCAL. No debe exponerse en una
red no confiable. Se acota con timeout de conexión y un tope de filas
devueltas. La capa de operaciones (`app/dataops/`) es más segura: genera
ella misma el SQL y pasa los valores como parámetros.
"""

from collections.abc import Callable, Sequence
from typing import Any

import psycopg
import pymysql

from app.models import DatabaseEngine

MAX_ROWS = 1000  # tope de filas devueltas para no saturar la UI/red


def _connect_postgres(*, host: str, port: int, user: str, password: str, dbname: str, timeout: int):
    return psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        connect_timeout=timeout,
        autocommit=True,
    )


def _connect_mysql(*, host: str, port: int, user: str, password: str, dbname: str, timeout: int):
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=dbname,
        connect_timeout=timeout,
        autocommit=True,
    )


_CONNECTORS: dict[DatabaseEngine, Callable] = {
    DatabaseEngine.POSTGRES: _connect_postgres,
    DatabaseEngine.MYSQL: _connect_mysql,
}


def _column_name(desc_entry: Any) -> str:
    """Nombre de columna de una entrada de `cursor.description`.

    psycopg expone objetos `Column` con atributo `.name`; PyMySQL devuelve
    tuplas DB-API clásicas (el nombre es el elemento 0).
    """
    name = getattr(desc_entry, "name", None)
    return name if name is not None else desc_entry[0]


def _to_jsonable(value: Any) -> Any:
    """Convierte tipos del driver (Decimal, date, bytes...) a algo serializable."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def run_query(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    dbname: str,
    sql: str,
    engine: DatabaseEngine = DatabaseEngine.POSTGRES,
    params: Sequence | None = None,
    many: bool = False,
    connect_timeout: int = 10,
) -> dict:
    """Ejecuta `sql` contra el motor indicado y devuelve el resultado.

    - Si la sentencia produce filas (SELECT, ... RETURNING):
      {columns, rows, rowcount, truncated}.
    - Si no (INSERT/CREATE/UPDATE/...): {columns: [], rows: [], rowcount, message}.
    - `params`: valores para placeholders `%s` (los usa la capa de
      operaciones; el SQL libre de la UI no los necesita).
    - `many=True`: `executemany` con una secuencia de tuplas (INSERT por lotes).

    Usa autocommit para que cada sentencia se aplique como en una consola SQL.
    Propaga los errores del driver (el endpoint los traduce a HTTP 400).
    """
    connect = _CONNECTORS[engine]
    connection = connect(
        host=host, port=port, user=user, password=password, dbname=dbname, timeout=connect_timeout
    )
    with connection as conn, conn.cursor() as cur:
        if many:
            cur.executemany(sql, params or [])
        else:
            cur.execute(sql, params)
        if cur.description is None:
            # Sentencia sin resultado tabular (INSERT/CREATE/...).
            return {
                "columns": [],
                "rows": [],
                "rowcount": cur.rowcount,
                "message": "OK",
            }
        columns = [_column_name(d) for d in cur.description]
        fetched = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(fetched) > MAX_ROWS
        rows = [[_to_jsonable(c) for c in row] for row in fetched[:MAX_ROWS]]
        return {
            "columns": columns,
            "rows": rows,
            "rowcount": len(rows),
            "truncated": truncated,
        }
