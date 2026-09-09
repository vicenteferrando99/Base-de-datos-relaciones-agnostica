# Guía de ejecución — cómo lanzarlo y qué esperar

> Recorrido completo de la herramienta, con los resultados esperados en cada
> paso. Las credenciales cloud se configuran una sola vez según `SETUP.md`;
> con solo Docker ya se puede probar TODO el flujo sin gastar un céntimo.

## 1. Arrancar en local

```bash
uv sync          # primera vez: instala dependencias (o `just install`)
just run         # = uv run uvicorn app.main:app --reload
```

**Qué esperar:** uvicorn escuchando en `http://localhost:8000`. Ahí está la
UI; en `/docs`, el Swagger. El proveedor activo al arrancar es el `PROVIDER`
del `.env` (se cambia en caliente desde el selector de la cabecera, sin
reiniciar).

## 2. Crear una instancia (pestaña Instancias)

Despliega "Nueva instancia", pon un nombre (minúsculas/dígitos/guiones),
contraseña de 8+ caracteres, y crea. Qué esperar según el proveedor activo:

| | `local_docker` | `aws_rds` | `gcp_cloudsql` |
|---|---|---|---|
| Tiempo hasta `available` | segundos (la 1ª vez descarga la imagen) | **5–15 min** | **5–15 min** |
| Estado inicial | `available` directamente | `creating` (la tarjeta se refresca sola cada 5 s) | `creating` + nota "Preparando base de datos y usuario..." |
| Host:puerto | `localhost:<puerto aleatorio>` | endpoint RDS al estar lista | IP pública al estar lista |
| Red | abierta (local) | abierta automáticamente (SG `cloudapi-dbaccess`, solo tu IP) | abierta automáticamente (authorized networks, solo tu IP) |
| Coste | gratis | free tier (`small`) | consume crédito de prueba |

Si aparece una **nota amarilla** en la tarjeta, léela: informa del progreso
del aprovisionamiento diferido (GCP) o de que la apertura de red falló y hay
que abrirla a mano (raro: solo si la API no pudo detectar su IP pública).

## 3. Consultar la instancia (pestaña Consultas)

Elige proveedor e instancia (o usa el botón **Consultar** de la tarjeta, que
te lleva con todo precargado). La UI rellena host, puerto, base de datos y
usuario según el motor y el proveedor; solo falta la contraseña (no se
guarda nunca: viaja en cada petición).

Dos modos:

- **SQL libre**: consola clásica. Escribes SQL del dialecto del motor y
  ejecutas (también con Ctrl+Enter). Qué esperar: tabla de resultados para
  SELECT; "OK (n afectadas)" para INSERT/CREATE/...; los errores del motor
  aparecen en rojo tal cual los devuelve el driver. Tope de 1000 filas.
- **Operaciones (agnóstico de motor)**: describes la operación con
  formularios (crear tabla / insertar filas / consultar) y el dialecto del
  motor genera el SQL. Qué esperar: un panel oscuro con el **SQL generado**
  (fíjate: comillas dobles y `NUMERIC` en Postgres; backticks y `DECIMAL` en
  MySQL) y debajo el resultado. La MISMA operación funciona en un Postgres
  de AWS y un MySQL de Docker — esa es la demo del TFM.

## 4. Lo mismo por API (para scripts o la memoria)

```bash
# Crear (201 + JSON con id, status, database...)
curl -X POST localhost:8000/instances -H 'Content-Type: application/json' \
  -d '{"name":"ventas","admin_password":"supersecret123"}'

# Polling hasta "status":"available" (cloud) — el host/port aparecen al final
curl localhost:8000/instances/<id>

# Operación agnóstica de motor (la respuesta incluye el SQL generado)
curl -X POST localhost:8000/data/select -H 'Content-Type: application/json' \
  -d '{"connection":{"engine":"postgres","host":"localhost","port":5432,
       "user":"dbadmin","password":"supersecret123","dbname":"ventas"},
       "query":{"table":"prestamos","limit":10}}'

# Borrar (204, idempotente)
curl -X DELETE localhost:8000/instances/<id>
```

También `scripts/demo_ingest.py`: el mismo script de ingesta funciona contra
los tres proveedores sin cambios (evidencia de agnosticismo de proveedor).

## 5. Tests y calidad

```bash
just            # tests rápidos, sin Docker  → "110 passed, 2 deselected"
just test-all   # incluye integración Docker → "112 passed"
just lint       # ruff limpio
```

## 6. Ejecutar desde la imagen (opcional)

```bash
just docker-run
```

**Qué esperar:** la imagen construyéndose (la primera vez tarda) y la misma UI
en `http://localhost:8000`, pero servida desde dentro del contenedor. Las
instancias Docker que crees aparecerán con host `172.17.0.1` —la puerta de
enlace, es decir el anfitrión visto desde el contenedor— en vez de
`localhost`; todo lo demás es idéntico. Es semántica POSIX: en Windows puede
requerir ajustes (ver `SETUP.md` §8.3).

## 7. Regla de oro del coste

Las pruebas cloud, efímeras: crear → probar → **borrar**. Comprueba siempre
que no queda nada:

```bash
just docker-clean                       # contenedores locales de la API
curl localhost:8000/instances           # con cada proveedor activo
```
