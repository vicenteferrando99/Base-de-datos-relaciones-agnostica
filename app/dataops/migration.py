"""
Migración de datos entre instancias, agnóstica de proveedor Y de motor.

Es la tercera aplicación del mismo patrón que ya usan el plano de control
(`app/adapters/`) y el plano de datos (`app/dataops/dialects.py`), y la que
cierra el argumento del TFM: si el esquema del origen se traduce al MODELO
ABSTRACTO (`ColumnType`) y es el dialecto del DESTINO quien genera el SQL
nativo, entonces mover una base de datos de un sitio a otro no depende de
qué proveedor ni de qué motor haya en cada extremo.

De ahí salen cuatro combinaciones por el mismo camino y sin código específico:

    AWS RDS  -> GCP Cloud SQL        (mismo motor, distinto proveedor)
    GCP      -> AWS                  (idem, en sentido inverso)
    Docker   -> cualquiera de las dos (subir un entorno local a la nube)
    Postgres -> MySQL                 (mismo o distinto proveedor)

La migración NO pasa por el proveedor: se conecta a los dos extremos con el
driver del motor correspondiente. Por eso funciona igual entre nubes que
entre una nube y un contenedor local, y por eso no necesita credenciales de
proveedor, solo las de las dos bases de datos.

ALCANCE DECLARADO (y sus límites, que son parte del resultado)

Se migran tablas, columnas, tipos, nulabilidad, clave primaria y filas.
NO se migran: índices secundarios, claves ajenas, restricciones CHECK o
UNIQUE, valores por defecto, secuencias/AUTO_INCREMENT, vistas, funciones,
triggers ni permisos. Una columna cuyo tipo nativo no tenga equivalente en
los nueve tipos del modelo abstracto NO se inventa: se reporta como no
soportada en la previsualización y el usuario decide.

Es deliberado que la previsualización sea un paso aparte: una migración es
una operación destructiva en potencia sobre el destino, y el usuario debe ver
qué se va a crear antes de que se cree.
"""

import logging
from dataclasses import dataclass, field

from app.dataops.dialects import get_dialect
from app.dataops.models import ColumnDef, ColumnType, RowsInsert, TableDefinition
from app.models import DatabaseEngine

logger = logging.getLogger(__name__)

# Tamaño de lote por defecto para el copiado. Compromiso entre número de
# viajes a la red y memoria: 500 filas de ancho razonable son unos pocos MB.
DEFAULT_BATCH_SIZE = 500

# Tipo nativo (lo que devuelve information_schema) -> tipo del modelo abstracto.
#
# Las claves están en minúsculas porque PostgreSQL las devuelve así y de MySQL
# se normalizan. Todo lo que no aparezca aquí se reporta como no soportado en
# lugar de traducirse a ciegas: preferimos que el usuario lo sepa antes de
# migrar a que descubra después que un `jsonb` se convirtió en texto.
_NATIVE_TO_ABSTRACT: dict[str, ColumnType] = {
    # --- enteros ---
    "smallint": ColumnType.INTEGER,
    "integer": ColumnType.INTEGER,
    "int": ColumnType.INTEGER,
    "mediumint": ColumnType.INTEGER,
    "bigint": ColumnType.BIGINT,
    # --- decimales exactos ---
    "numeric": ColumnType.DECIMAL,
    "decimal": ColumnType.DECIMAL,
    # --- coma flotante ---
    "double precision": ColumnType.FLOAT,
    "double": ColumnType.FLOAT,
    "real": ColumnType.FLOAT,
    "float": ColumnType.FLOAT,
    # --- booleano ---
    "boolean": ColumnType.BOOLEAN,
    "bool": ColumnType.BOOLEAN,
    # --- texto ---
    "character varying": ColumnType.STRING,
    "varchar": ColumnType.STRING,
    "character": ColumnType.STRING,
    "char": ColumnType.STRING,
    "text": ColumnType.TEXT,
    "longtext": ColumnType.TEXT,
    "mediumtext": ColumnType.TEXT,
    "tinytext": ColumnType.TEXT,
    # --- fechas ---
    "date": ColumnType.DATE,
    "timestamp without time zone": ColumnType.TIMESTAMP,
    "timestamp with time zone": ColumnType.TIMESTAMP,
    "timestamp": ColumnType.TIMESTAMP,
    "datetime": ColumnType.TIMESTAMP,
}

# `tinyint` merece un comentario aparte porque es una ASIMETRÍA REAL que la
# migración destapa y no puede resolver: MySQL implementa BOOLEAN como
# TINYINT(1), de modo que al leer el esquema de MySQL es imposible distinguir
# un booleano de un entero pequeño — `information_schema` devuelve `tinyint`
# en ambos casos. Se traduce a INTEGER (la lectura conservadora: nunca pierde
# información, a lo sumo la generaliza) y se avisa. Consecuencia práctica: un
# BOOLEAN de PostgreSQL que viaje a MySQL y vuelva regresa como INTEGER.
_NATIVE_TO_ABSTRACT["tinyint"] = ColumnType.INTEGER

_LOSSY_NOTES: dict[str, str] = {
    "tinyint": (
        "MySQL representa BOOLEAN como TINYINT(1) y no es distinguible de un "
        "entero pequeño: se migra como 'integer'."
    ),
    "timestamp with time zone": (
        "La zona horaria no se preserva: el modelo abstracto solo tiene 'timestamp' sin zona."
    ),
    "character": "Se migra como 'string' (VARCHAR): el relleno de CHAR se pierde.",
    "char": "Se migra como 'string' (VARCHAR): el relleno de CHAR se pierde.",
}


@dataclass
class UnsupportedColumn:
    """Columna cuyo tipo nativo no tiene equivalente en el modelo abstracto."""

    table: str
    column: str
    native_type: str


@dataclass
class SchemaReport:
    """Resultado de inspeccionar el esquema del origen."""

    tables: list[TableDefinition] = field(default_factory=list)
    unsupported: list[UnsupportedColumn] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def table(self, name: str) -> TableDefinition | None:
        return next((t for t in self.tables if t.name == name), None)


@dataclass
class TableResult:
    """Resultado de migrar una tabla."""

    table: str
    created: bool
    rows: int
    sql: str


@dataclass
class MigrationReport:
    """Resultado completo de una migración."""

    tables: list[TableResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


# --------------------------------------------------------------------------
# Inspección del esquema de origen
# --------------------------------------------------------------------------

# `information_schema` está normalizado por el estándar SQL y ambos motores lo
# implementan, así que la MISMA consulta sirve para los dos. Es el motivo por el
# que esta capa no necesita un método por dialecto: la asimetría entre motores
# aparece al ESCRIBIR (tipos nativos, quoting), no al leer el catálogo.
_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = %s
ORDER BY table_name, ordinal_position
"""

_TABLES_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = %s AND table_type = 'BASE TABLE'
"""

_PRIMARY_KEYS_SQL = """
SELECT tc.table_name, kcu.column_name
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema = %s
"""


def _schema_name(engine: DatabaseEngine, dbname: str) -> str:
    """Nombre del esquema donde viven las tablas del usuario.

    Otra asimetría de vocabulario: en PostgreSQL las tablas del usuario viven
    en el esquema `public` dentro de la base de datos; en MySQL no hay esquemas
    separados de la base de datos, así que `table_schema` ES el nombre de la BD.
    """
    return "public" if engine == DatabaseEngine.POSTGRES else dbname


def existing_tables(connection, engine: DatabaseEngine, dbname: str) -> set[str]:
    """Tablas que ya existen en una base de datos.

    Se usa contra el DESTINO para detectar colisiones antes de escribir. Sin
    esta comprobación, intentar crear una tabla que ya existe produce un error
    crudo del driver (`relation ... already exists` / `Table ... already
    exists`) que no le dice al usuario qué hacer: la decisión real es si quiere
    **fusionar** en la tabla existente o si se ha equivocado de destino, y eso
    debe preguntarse antes, no descubrirse a mitad.
    """
    schema = _schema_name(engine, dbname)
    with connection.cursor() as cur:
        cur.execute(_TABLES_SQL, (schema,))
        return {row[0] for row in cur.fetchall()}


def introspect(connection, engine: DatabaseEngine, dbname: str) -> SchemaReport:
    """Lee el esquema del origen y lo traduce al modelo abstracto."""
    schema = _schema_name(engine, dbname)
    report = SchemaReport()

    with connection.cursor() as cur:
        cur.execute(_COLUMNS_SQL, (schema,))
        raw_columns = cur.fetchall()
        cur.execute(_PRIMARY_KEYS_SQL, (schema,))
        raw_pks = cur.fetchall()

    primary_keys: dict[str, set[str]] = {}
    for table_name, column_name in raw_pks:
        primary_keys.setdefault(table_name, set()).add(column_name)

    seen_notes: set[str] = set()
    columns_by_table: dict[str, list[ColumnDef]] = {}

    for table_name, column_name, data_type, is_nullable in raw_columns:
        native = str(data_type).strip().lower()
        abstract = _NATIVE_TO_ABSTRACT.get(native)
        if abstract is None:
            report.unsupported.append(
                UnsupportedColumn(table=table_name, column=column_name, native_type=native)
            )
            continue
        if native in _LOSSY_NOTES and native not in seen_notes:
            seen_notes.add(native)
            report.warnings.append(_LOSSY_NOTES[native])
        columns_by_table.setdefault(table_name, []).append(
            ColumnDef(
                name=column_name,
                type=abstract,
                primary_key=column_name in primary_keys.get(table_name, set()),
                nullable=str(is_nullable).upper() != "NO",
            )
        )

    for table_name, columns in columns_by_table.items():
        report.tables.append(TableDefinition(name=table_name, columns=columns))

    report.tables.sort(key=lambda t: t.name)
    return report


# --------------------------------------------------------------------------
# Copia de datos
# --------------------------------------------------------------------------


def _select_all_sql(engine: DatabaseEngine, table: TableDefinition) -> str:
    """SELECT explícito de las columnas migrables, en orden estable.

    No se usa `SELECT *`: las columnas no soportadas se han descartado en la
    inspección y el orden debe coincidir exactamente con el del INSERT.
    """
    dialect = get_dialect(engine)
    cols = ", ".join(dialect.quote(c.name) for c in table.columns)
    return f"SELECT {cols} FROM {dialect.quote(table.name)}"


def _copy_table(
    *,
    source_connection,
    target_connection,
    source_engine: DatabaseEngine,
    target_engine: DatabaseEngine,
    table: TableDefinition,
    batch_size: int,
) -> int:
    """Copia las filas de una tabla por lotes. Devuelve el número copiado.

    Se lee con `fetchmany` en bucle en lugar de traerlo todo de golpe. Aun así
    el driver de MySQL almacena el resultado completo en el cliente por
    defecto, de modo que para tablas muy grandes esto seguiría siendo pesado:
    es una limitación conocida y aceptable para el alcance del trabajo.
    """
    column_names = [c.name for c in table.columns]
    target_dialect = get_dialect(target_engine)
    copied = 0

    with source_connection.cursor() as src_cur, target_connection.cursor() as dst_cur:
        src_cur.execute(_select_all_sql(source_engine, table))
        while True:
            batch = src_cur.fetchmany(batch_size)
            if not batch:
                break
            rows = [dict(zip(column_names, row, strict=True)) for row in batch]
            sql, params = target_dialect.insert(RowsInsert(table=table.name, rows=rows))
            dst_cur.executemany(sql, params)
            copied += len(rows)

    return copied


def migrate(
    *,
    source_connection,
    target_connection,
    source_engine: DatabaseEngine,
    target_engine: DatabaseEngine,
    schema: SchemaReport,
    tables: list[str] | None = None,
    create_tables: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> MigrationReport:
    """Recrea el esquema en el destino y copia los datos.

    `schema` viene de `introspect()` sobre el origen. `tables` acota la
    selección; None migra todas. Si `create_tables` es False se asume que las
    tablas ya existen en el destino y solo se copian las filas.

    Cada tabla se confirma por separado: una migración a medias es más útil
    que una transacción gigante que se pierde entera por la última tabla, y el
    informe dice exactamente hasta dónde se llegó.
    """
    selected = schema.tables if tables is None else [t for t in schema.tables if t.name in tables]
    report = MigrationReport(warnings=list(schema.warnings))

    if schema.unsupported:
        report.warnings.append(
            f"{len(schema.unsupported)} columna(s) con tipo no soportado quedan fuera "
            "de la migración; consulta la previsualización para verlas."
        )

    target_dialect = get_dialect(target_engine)

    for table in selected:
        create_sql = target_dialect.create_table(table)
        if create_tables:
            with target_connection.cursor() as cur:
                cur.execute(create_sql)
            logger.info("Tabla %s creada en el destino", table.name)

        rows = _copy_table(
            source_connection=source_connection,
            target_connection=target_connection,
            source_engine=source_engine,
            target_engine=target_engine,
            table=table,
            batch_size=batch_size,
        )
        report.tables.append(
            TableResult(table=table.name, created=create_tables, rows=rows, sql=create_sql)
        )

    return report
