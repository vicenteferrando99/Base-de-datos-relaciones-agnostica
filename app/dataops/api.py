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

- `POST /migrate/preview` y `POST /migrate` — migración entre dos instancias
  cualesquiera (ver `app/dataops/migration.py`). Van en dos pasos a propósito:
  previsualizar es de solo lectura sobre el origen, mientras que migrar
  escribe en el destino.

En todos los casos los datos de conexión viajan en cada petición: la API no
almacena credenciales de instancias.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import query
from app.dataops import migration
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


# ---------- Migración entre instancias (agnóstica de proveedor y de motor) ----------


class MigrationPreviewPayload(BaseModel):
    """El destino es opcional: si se aporta, se comprueba (solo lectura) qué
    tablas ya existen allí, que es lo que determina si la migración creará
    tablas nuevas o fusionará en las existentes."""

    source: ConnectionParams
    target: ConnectionParams | None = None


class MigrationPayload(BaseModel):
    source: ConnectionParams
    target: ConnectionParams
    #: Tablas a migrar. None o lista vacía => todas las del origen.
    tables: list[str] | None = None
    #: Si el destino ya tiene las tablas creadas, ponerlo a False y solo se copian filas.
    create_tables: bool = True
    batch_size: int = Field(default=migration.DEFAULT_BATCH_SIZE, ge=1, le=10000)


def _connect(conn: ConnectionParams):
    """Abre conexión traduciendo cualquier fallo de red/driver a HTTP 400."""
    try:
        return query.connect(
            engine=conn.engine,
            host=conn.host,
            port=conn.port,
            user=conn.user,
            password=conn.password,
            dbname=conn.dbname,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo conectar: {exc}") from exc


@router.post("/migrate/preview")
def preview_migration(payload: MigrationPreviewPayload) -> dict:
    """Inspecciona el esquema del origen y devuelve el plan, SIN tocar el destino.

    Paso deliberadamente separado de la ejecución: una migración escribe en el
    destino, y el usuario debe poder ver antes qué tablas se van a crear, con
    qué tipos y qué columnas quedan fuera por no ser representables en el
    modelo abstracto.
    """
    connection = _connect(payload.source)
    try:
        schema = migration.introspect(connection, payload.source.engine, payload.source.dbname)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        connection.close()

    # Si se aporta el destino, se mira qué tablas ya tiene. Es solo lectura y
    # permite que la interfaz decida por el usuario entre crear y fusionar.
    already_there: list[str] = []
    if payload.target is not None:
        target_conn = _connect(payload.target)
        try:
            present = migration.existing_tables(
                target_conn, payload.target.engine, payload.target.dbname
            )
            already_there = sorted(present & {t.name for t in schema.tables})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            target_conn.close()

    return {
        "target_existing": already_there,
        "tables": [
            {
                "name": t.name,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type.value,
                        "primary_key": c.primary_key,
                        "nullable": c.nullable,
                    }
                    for c in t.columns
                ],
            }
            for t in schema.tables
        ],
        "unsupported": [
            {"table": u.table, "column": u.column, "native_type": u.native_type}
            for u in schema.unsupported
        ],
        "warnings": schema.warnings,
    }


@router.post("/migrate")
def run_migration(payload: MigrationPayload) -> dict:
    """Migra esquema y datos del origen al destino.

    Funciona entre proveedores distintos y entre motores distintos por la misma
    vía: el esquema del origen se traduce al modelo abstracto y es el dialecto
    del destino quien genera su SQL nativo. La migración no habla con ningún
    SDK de proveedor — solo con las dos bases de datos.
    """
    source_conn = _connect(payload.source)
    target_conn = None
    try:
        schema = migration.introspect(source_conn, payload.source.engine, payload.source.dbname)
        if not schema.tables:
            raise HTTPException(
                status_code=400,
                detail="El origen no tiene ninguna tabla migrable.",
            )
        target_conn = _connect(payload.target)

        # Colisiones ANTES de escribir nada. Sin esto, el fallo llega como un
        # error crudo del driver a mitad de la migración y con parte del
        # trabajo ya confirmado.
        if payload.create_tables:
            selected = set(payload.tables or [t.name for t in schema.tables])
            present = migration.existing_tables(
                target_conn, payload.target.engine, payload.target.dbname
            )
            collisions = sorted(present & selected)
            if collisions:
                cuantas = "esta tabla" if len(collisions) == 1 else "estas tablas"
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"El destino ya tiene {cuantas}: {', '.join(collisions)}. "
                        "Desmarca «Crear las tablas en el destino» para añadir las "
                        "filas a las tablas existentes, o elige otro destino."
                    ),
                )

        report = migration.migrate(
            source_connection=source_conn,
            target_connection=target_conn,
            source_engine=payload.source.engine,
            target_engine=payload.target.engine,
            schema=schema,
            tables=payload.tables or None,
            create_tables=payload.create_tables,
            batch_size=payload.batch_size,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        source_conn.close()
        if target_conn is not None:
            target_conn.close()

    return {
        "tables": [
            {"table": t.table, "created": t.created, "rows": t.rows, "sql": t.sql}
            for t in report.tables
        ],
        "total_rows": report.total_rows,
        "warnings": report.warnings,
    }
