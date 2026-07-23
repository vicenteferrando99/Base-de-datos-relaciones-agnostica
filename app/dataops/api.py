"""
Endpoints HTTP del DATA PLANE.

Separados de `app/main.py` para que aquel quede dedicado al control plane
(gestión de instancias). Aquí viven:

- `POST /query` — SQL libre contra una instancia (consola de la UI). Depende
  del motor solo para elegir driver; el SQL lo escribe el usuario.
- `POST /data/tables|rows|select` — capa de operaciones agnóstica de MOTOR:
  el cliente describe la operación en el modelo abstracto y el dialecto del
  motor genera el SQL. Las respuestas incluyen el SQL generado (`sql`) a
  propósito: es la evidencia del agnosticismo (misma petición, distinto SQL
  según el motor) y facilita la demo y la memoria.

En todos los casos los datos de conexión viajan en cada petición: la API no
almacena credenciales de instancias.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import query
from app.dataops.dialects import get_dialect
from app.dataops.models import RowsInsert, SelectQuery, TableDefinition
from app.models import DatabaseEngine

router = APIRouter()


class ConnectionParams(BaseModel):
    """Datos de conexión a una instancia. Se envían en cada petición."""

    engine: DatabaseEngine = DatabaseEngine.POSTGRES
    host: str
    port: int
    user: str
    password: str
    dbname: str


def _execute(conn: ConnectionParams, sql: str, *, params: Any = None, many: bool = False) -> dict:
    """Ejecuta contra la instancia y traduce errores de driver/red a HTTP 400."""
    try:
        return query.run_query(
            engine=conn.engine,
            host=conn.host,
            port=conn.port,
            user=conn.user,
            password=conn.password,
            dbname=conn.dbname,
            sql=sql,
            params=params,
            many=many,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- SQL libre (consola) ----------


class QueryRequest(ConnectionParams):
    sql: str


@router.post("/query", include_in_schema=False)
def run_sql(req: QueryRequest) -> dict:
    """Ejecuta SQL libre (lo usa la pestaña Consultas). Ver aviso en app/query.py."""
    return _execute(req, req.sql)  # QueryRequest ES un ConnectionParams + sql


# ---------- Operaciones agnósticas de motor ----------


class CreateTablePayload(BaseModel):
    connection: ConnectionParams
    table: TableDefinition


class InsertRowsPayload(BaseModel):
    connection: ConnectionParams
    insert: RowsInsert


class SelectPayload(BaseModel):
    connection: ConnectionParams
    query: SelectQuery


@router.post("/data/tables", status_code=201)
def create_table(payload: CreateTablePayload) -> dict:
    """Crea una tabla descrita en el modelo abstracto (agnóstico de motor)."""
    sql = get_dialect(payload.connection.engine).create_table(payload.table)
    result = _execute(payload.connection, sql)
    return {"sql": sql, **result}


@router.post("/data/rows", status_code=201)
def insert_rows(payload: InsertRowsPayload) -> dict:
    """Inserta filas (por lotes) descritas en el modelo abstracto."""
    sql, params = get_dialect(payload.connection.engine).insert(payload.insert)
    result = _execute(payload.connection, sql, params=params, many=True)
    return {"sql": sql, **result}


@router.post("/data/select")
def select_rows(payload: SelectPayload) -> dict:
    """Consulta filas con la operación SELECT del modelo abstracto."""
    sql, params = get_dialect(payload.connection.engine).select(payload.query)
    result = _execute(payload.connection, sql, params=params or None)
    return {"sql": sql, **result}
