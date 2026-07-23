# Data plane: diseño de la capa de operaciones agnóstica de motor

> Documento de diseño de `app/dataops/`. Material directo para las secciones
> de diseño e implementación de la memoria.

## 1. Qué problema resuelve (y cuál no)

El proyecto abstrae dos dimensiones **ortogonales**:

| Dimensión | Plano | Quién la resuelve | Ejemplo |
|---|---|---|---|
| Proveedor (AWS/GCP/Docker) | Control plane | `app/adapters/` | crear una instancia es distinto en boto3 y en Cloud SQL Admin API |
| Motor (PostgreSQL/MySQL) | Data plane | `app/dataops/` | `TIMESTAMP` sano es `TIMESTAMP` en Postgres y `DATETIME` en MySQL |

El data plane ya era agnóstico de **proveedor** por naturaleza (un cliente
PostgreSQL conecta igual a RDS, Cloud SQL o un contenedor). No era agnóstico
de **motor**: el SQL de Postgres no siempre es SQL válido de MySQL. Esta capa
elimina esa dependencia para un **conjunto cerrado de operaciones**.

**Acotación deliberada:** NO se parsea ni traduce SQL arbitrario. El usuario
que escribe SQL libre (consola de la UI) escribe en el dialecto de su motor.
La traducción general de dialectos (AST, funciones, tipos) es un problema del
tamaño de otro TFM y queda como trabajo futuro.

## 2. Arquitectura: el mismo patrón, en la otra dimensión

```
Control plane                          Data plane
─────────────                          ──────────
DatabaseAdapter (contrato)             SqlDialect (contrato)
├── LocalDockerAdapter                 ├── PostgresDialect
├── AwsRdsAdapter                      └── MySqlDialect
└── GcpCloudSqlAdapter
registry (@register + dict)            get_dialect (dict)
modelo unificado (models.py)           modelo de operaciones (dataops/models.py)
InstanceSize.SMALL -> db.t3.micro      ColumnType.STRING -> VARCHAR(255)
```

La simetría es deliberada y es el argumento defensivo central: si el patrón
Adapter funciona para abstraer proveedores, también funciona para abstraer
motores. El registry de dialectos es un simple dict (no el decorador del
control plane) porque los dialectos no tienen estado ni configuración —
aplicar el mismo mecanismo habría sido sobrediseño.

## 3. El modelo de operaciones

Tres operaciones, deliberadamente básicas (sin joins, agregaciones ni ALTER):

| Operación | Endpoint | Modelo |
|---|---|---|
| Crear tabla | `POST /data/tables` | `TableDefinition` (columnas con tipos abstractos, PK, NOT NULL) |
| Insertar filas | `POST /data/rows` | `RowsInsert` (lote de filas con las mismas claves) |
| Consultar | `POST /data/select` | `SelectQuery` (columnas, filtros AND, orden, límite) |

Cada respuesta incluye el campo `sql` con el SQL generado. Es la evidencia
observable del agnosticismo: la misma petición produce
`CREATE TABLE "prestamos" (...)` con `engine=postgres` y
``CREATE TABLE `prestamos` (...)`` con `engine=mysql`.

### Tipos abstractos y su mapeo

| `ColumnType` | PostgreSQL | MySQL | Nota |
|---|---|---|---|
| `integer` | INTEGER | INTEGER | |
| `bigint` | BIGINT | BIGINT | |
| `decimal` | NUMERIC(18, 4) | DECIMAL(18, 4) | exacto; para importes |
| `float` | DOUBLE PRECISION | DOUBLE | |
| `boolean` | BOOLEAN | BOOLEAN | en MySQL es alias de TINYINT(1) |
| `string` | VARCHAR(255) | VARCHAR(255) | texto corto indexable |
| `text` | TEXT | TEXT | en MySQL no puede ser PK |
| `date` | DATE | DATE | |
| `timestamp` | TIMESTAMP | DATETIME | TIMESTAMP de MySQL tiene rango 1970-2038 y semántica TZ distinta |

Las decisiones "opinadas" (precisión fija del DECIMAL, longitud fija del
VARCHAR) son el precio de la abstracción: menos control fino a cambio de un
vocabulario común. Mismo trade-off que `InstanceSize.SMALL` en el control
plane. Documentarlo así en la memoria.

## 4. Seguridad (doble barrera)

El riesgo clásico de generar SQL es la inyección. Defensa en dos capas:

1. **Valores → parámetros.** Ningún valor de usuario se interpola en el SQL:
   viajan como placeholders `%s` del driver (psycopg y PyMySQL comparten
   estilo). El driver escapa.
2. **Identificadores → validación + quoting.** Los nombres de tabla/columna
   no se pueden parametrizar en SQL. Se validan en el modelo Pydantic con el
   patrón `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$` (una petición con
   `"table": "t; DROP TABLE t"` devuelve 422 y nunca llega a generar SQL) y,
   además, cada dialecto los quotea (`"t"` / `` `t` ``).

El `LIMIT` se interpola pero está validado como entero 1–1000 por Pydantic.

Contraste con la consola de SQL libre (`POST /query`): esa SÍ ejecuta SQL
arbitrario y por eso es herramienta de demo local con avisos explícitos. La
capa de operaciones es el camino "seguro por construcción".

## 5. Conexión: driver por motor

`app/query.py` elige driver según el motor (mismo patrón lookup):

| Motor | Driver | Nota |
|---|---|---|
| postgres | psycopg 3 | |
| mysql | PyMySQL | requiere `cryptography` para el auth por defecto de MySQL 8 (`caching_sha2_password`) |

Ambos implementan DB-API 2.0, así que ejecución, `executemany` (inserciones
por lotes) y lectura de resultados comparten código. La única asimetría real
encontrada: `cursor.description` de psycopg da objetos `Column` con `.name`;
PyMySQL da tuplas (nombre en el índice 0). Anotar en la memoria como ejemplo
de fricción de detalle incluso entre drivers "estándar".

Las credenciales de conexión viajan en cada petición y no se almacenan.

## 6. Evidencia empírica (E2E, jul 2026)

Flujo completo ejecutado contra contenedores Docker reales con la MISMA
petición JSON (solo cambia `engine` en la conexión):

| Paso | postgres | mysql |
|---|---|---|
| CREATE | `CREATE TABLE "prestamos" (... "alta" DATE ...)` | ``CREATE TABLE `prestamos` (... `alta` DATE ...)`` |
| INSERT ×3 | `VALUES (%s, %s, %s, %s)` — rowcount 3 | ídem — rowcount 3 |
| SELECT filtrado | `[['Carla', '2500.7500'], ['Ana', '1200.5000']]` | idéntico |

Reproducible con la UI (pestaña Consultas → "Operaciones (agnóstico de
motor)") o con `curl` (ver README).

## 7. Trabajo futuro de esta capa

- Ampliar el conjunto de operaciones (joins, agregaciones, ALTER, índices).
- Más motores (SQL Server, MariaDB): añadir un dialecto = rellenar un TYPE_MAP
  y un método `quote` (~20 líneas).
- Traducción de SQL arbitrario entre dialectos: fuera del alcance del TFM
  (ver CLAUDE.md §10.2).
