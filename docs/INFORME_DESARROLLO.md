# Informe de desarrollo — qué se ha construido y por qué

> Registro cronológico del desarrollo del TFM, con las decisiones tomadas y
> la evidencia de verificación de cada bloque. Complementa a `MEMORIA.md`
> (andamiaje de la memoria): esto es la crónica; aquello, la estructura del
> documento final. Cada bloque referencia su commit.

## Visión de conjunto

El proyecto resuelve **dos dependencias ortogonales** con el mismo patrón
(Adapter + registry):

| Dependencia | Plano | Solución | Código |
|---|---|---|---|
| de **proveedor** (AWS/GCP/Docker) | control plane (crear/gestionar instancias) | `DatabaseAdapter` + un adaptador por proveedor | `app/adapters/` |
| de **motor** (PostgreSQL/MySQL) | data plane (operar con los datos) | `SqlDialect` + un dialecto por motor | `app/dataops/` |

Tesis demostrada: cambiar de proveedor (o de motor, para el conjunto acotado
de operaciones) es una decisión de **configuración**, no de código.

## Bloque 1 — Núcleo del control plane (commits `796cd28` … `79027db`)

Trabajo previo a este informe, resumido para contexto:

- Modelo unificado (`app/models.py`): tipos abstractos (`InstanceSize`,
  `InstanceRegion`, `DatabaseEngine`, `InstanceStatus`) que cada adaptador
  mapea a lo nativo. Nunca se exponen nombres de proveedor.
- Contrato `DatabaseAdapter` (4 métodos CRUD) + **registry plugin** con
  decorador `@register`: añadir un proveedor no toca `main.py` (inversión de
  dependencias, argumento de extensibilidad).
- Tres adaptadores: `local_docker` (labels de ownership), `aws_rds` (tags,
  multi-región, moto en tests) y `gcp_cloudsql` (userLabels, aprovisionamiento
  diferido de BD/usuario por el ciclo asíncrono de Cloud SQL). Los tres
  replican el patrón `managed-by=cloud-db-api` + `instance-id`.
- API REST (CRUD `/instances`, `PUT /provider` en caliente), UI web
  (Tailwind + Alpine, sin build), CI en GitHub Actions, justfile.
- Smoke tests reales superados en AWS y GCP; consola SQL inicial (`/query`,
  commit `6db36b1`, solo Postgres entonces).

## Bloque 2 — Capa agnóstica de MOTOR (commit `5c9b011`)

**Decisión de alcance** (jul 2026, decisión propia con libertad de tutores):
subir al TFM el agnosticismo de motor en su versión **acotada** — un conjunto
CERRADO de operaciones (crear tabla, insertar, consultar con filtros)
descritas en un modelo abstracto, con un dialecto por motor que genera el SQL
nativo. La traducción de SQL arbitrario (parsear dialectos) queda
explícitamente como trabajo futuro: es otro TFM.

Qué se construyó:

- `app/dataops/models.py`: `TableDefinition`, `RowsInsert`, `SelectQuery`,
  9 tipos de columna abstractos. Identificadores validados con patrón
  estricto (anti-inyección) — una petición con `"table": "t; DROP TABLE t"`
  muere con 422 antes de generar SQL.
- `app/dataops/dialects.py`: contrato `SqlDialect` + `PostgresDialect` /
  `MySqlDialect`. Los valores SIEMPRE van parametrizados (`%s`); los
  identificadores, validados y quoteados (doble barrera de seguridad).
- `app/dataops/api.py`: `POST /data/tables|rows|select`. Las respuestas
  incluyen el SQL generado — la evidencia observable del agnosticismo.
- `app/query.py` multi-motor: driver por engine (psycopg / PyMySQL) tras un
  lookup, parámetros y `executemany`. La consola SQL pasó a funcionar con
  instancias MySQL.
- Modelo: `name` validado con el patrón común a los tres proveedores;
  `InstanceInfo.database` nuevo (el nombre real de la BD difiere de `name`
  en AWS y GCP).
- UI: la pestaña Consultas ganó dos modos (SQL libre / constructor de
  operaciones) y el panel "SQL generado por el dialecto".

**Verificación:** tests de dialectos como funciones puras (operación → SQL
esperado por motor) + E2E real: la misma petición JSON contra un Postgres y
un MySQL en Docker → SQL distinto, resultado idéntico (tabla en
`DATA_PLANE.md` §6).

## Bloque 3 — Red automática (commit `d420323`)

**Problema:** las instancias cloud nacen con la red cerrada; conectarse
exigía abrir Security Group (AWS) o authorized networks (GCP) a mano en la
consola del proveedor.

**Solución:** el adaptador abre la red al crear. `app/network.py` detecta la
IP pública de la máquina que ejecuta la API (es el servidor quien conecta
con la BBDD, no el navegador). AWS: SG `cloudapi-dbaccess` reutilizable en
la Default VPC con ingress del puerto del motor desde `<IP>/32` +
`PubliclyAccessible`; se respeta `AWS_SECURITY_GROUP_ID` si el usuario lo
define. GCP: `authorizedNetworks` en el body de creación. Si la IP no se
puede detectar, la instancia se crea igual (red cerrada) y se avisa en
`note` — degradación elegante.

**Verificación:** 10 tests nuevos (SG con la regla exacta vía moto,
reutilización, puerto MySQL, degradación, override del usuario; body de GCP).
Pendiente el smoke real en nube (la lógica está cubierta con mocks).

## Bloque 4 — Empaquetado (commit `81f8e4a`)

- `Dockerfile` multi-stage con uv (capa de dependencias cacheada), non-root.
- Hallazgo técnico: "localhost" dentro del contenedor no es el anfitrión →
  nuevo `Settings.docker_instance_host`, que debe apuntar a la puerta de
  enlace de la red para que los datos de conexión sean alcanzables.
- Receta `just docker-build` (y después `docker-run`).

> **Retirado del alcance en septiembre de 2026.** Este bloque incluía
> originalmente un despliegue en Kubernetes local (cluster `kind` + Helm
> chart en `deploy/`, recetas `just k8s-*`). Se eliminó del proyecto por
> decisión de alcance: no se iba a mantener ni revisar, y sostener en la
> memoria un componente sin validar habría sido peor que no tenerlo. El
> empaquetado en imagen sí se conserva, porque es independiente y sí se usa.
> El código retirado sigue disponible en el historial de git.

## Correcciones por el camino (no planificadas, encontradas trabajando)

- `.env` apuntaba al socket de Docker Desktop, ya inexistente en la máquina
  (se pasó a Docker Engine clásico). Habría roto `local_docker`.
- El README anunciaba el estado de hace dos meses ("aws_rds en diseño").
- La UI adivinaba mal el nombre de BD y el usuario/puerto para MySQL y GCP;
  ahora la API informa (`database`) y la UI decide por motor+proveedor.

## Estado final

- **112 tests** (unitarios, dialectos puros, moto, mocks GCP, endpoints,
  integración Docker real) + CI verde.
- Documentos técnicos: `MEMORIA.md` (esqueleto de la memoria),
  `DATA_PLANE.md` (diseño del data plane y de la migración),
  `SETUP.md` (credenciales), `GUIA_EJECUCION.md` (cómo ejecutar y qué
  esperar) y este informe.
- Desarrollo técnico COMPLETO. Queda la Fase 5: evaluación (métricas de
  desacoplamiento, costes) y redacción de la memoria; y el smoke real de la
  red automática en AWS/GCP.
