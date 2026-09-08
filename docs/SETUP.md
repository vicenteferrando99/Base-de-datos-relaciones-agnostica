# Guía de configuración (pasos manuales)

Este documento recoge **todo lo que el usuario debe configurar a mano** para
usar la API con cada proveedor. Es deliberadamente exhaustivo: una de las
conclusiones del TFM es que parte de la dependencia de proveedor se desplaza
de la *aplicación* a la *configuración del entorno* (credenciales, redes,
proyectos), y conviene tenerla documentada.

> Estado actual: credenciales y proyecto siguen siendo configuración
> **manual** fuera de la API. La **apertura de red** (Security Group en AWS,
> authorized networks en GCP) ya es automática desde jul 2026 — ver §4.3 y
> §5.3. Gestionar credenciales/proyecto desde la herramienta sigue como
> línea de mejora.

---

## 1. Requisitos de software

El proyecto se desarrolló en Ubuntu y se ha verificado también en Windows 11.
El **código no cambia** entre sistemas; solo cambia cómo se instalan las
herramientas y dónde guardan sus credenciales.

| Herramienta | Para qué | Instalación (Ubuntu) | Instalación (Windows) |
|---|---|---|---|
| Python 3.11+ | Ejecutar la API | (del sistema / `uv python install`) | `uv python install` |
| [uv](https://docs.astral.sh/uv/) | Gestor de dependencias | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `winget install astral-sh.uv` |
| Docker | Adaptador `local_docker` | Docker Engine o Docker Desktop | Docker Desktop |
| [just](https://github.com/casey/just) | Atajos de tareas (opcional) | `sudo snap install just` | `winget install Casey.Just` |
| gcloud CLI | Setup de GCP | `sudo snap install google-cloud-cli --classic` | `winget install Google.CloudSDK` |
| AWS CLI | Setup/gestión de AWS (opcional) | `sudo snap install aws-cli --classic` | `winget install Amazon.AWSCLI` |
| psql | SQL contra las instancias (opcional: la UI ya trae consola) | `sudo apt install postgresql-client` | `winget install PostgreSQL.PostgreSQL.16` |
| helm / kind | Solo para la fase 4 (Kubernetes) | paquetes del sistema | `winget install Helm.Helm` / `winget install Kubernetes.kind` |

Instalar dependencias del proyecto:

```bash
uv sync
```

> `uv sync` instala **solo dependencias de Python**. No crea el `.env` ni
> configura credenciales: son los pasos manuales de las secciones siguientes.

---

## 2. Configuración común (`.env`)

Copiar la plantilla y ajustar:

```bash
cp .env.example .env
```

Variables relevantes:

| Variable | Proveedor | Descripción |
|---|---|---|
| `PROVIDER` | todos | Adaptador activo: `local_docker`, `aws_rds`, `gcp_cloudsql` |
| `DOCKER_HOST` | local_docker | Socket del daemon (necesario con Docker Desktop en Linux) |
| `AWS_REGION` | aws_rds | Región nativa por defecto (informativo; la región se elige por petición) |
| `GCP_PROJECT` | gcp_cloudsql | **Obligatorio** para GCP. ID del proyecto |
| `GCP_REGION` | gcp_cloudsql | Región nativa por defecto (informativo) |

> Las **credenciales** (AWS access keys, GCP) **NO** van en `.env`. Cada SDK
> las lee de su ubicación estándar (ver abajo). Esto evita filtrarlas.

---

## 3. Adaptador `local_docker`

Requisitos:

1. Docker instalado y el **daemon corriendo**.
2. `DOCKER_HOST` normalmente **no hay que tocarlo**: el SDK detecta solo la
   ruta correcta en cada sistema.

   | Entorno | Socket | ¿Configurar? |
   |---|---|---|
   | Linux + Docker Engine | `/var/run/docker.sock` | No |
   | Linux + Docker Desktop | `~/.docker/desktop/docker.sock` | **Sí**, a mano |
   | Windows + Docker Desktop | `npipe:////./pipe/docker_engine` | No |
   | macOS + Docker Desktop | `~/.docker/run/docker.sock` | No |

3. El único caso que exige fijarlo es **Docker Desktop en Linux**:
   ```
   DOCKER_HOST=unix:///home/<usuario>/.docker/desktop/docker.sock
   ```

> **Aviso al migrar de sistema:** si reutilizas un `.env` de Linux en Windows,
> el valor `DOCKER_HOST=unix://...` rompe el adaptador. En Windows debe quedar
> comentado o ausente.

Conexión a las instancias: directa a `localhost:<puerto>` (sin firewalls).

---

## 4. Adaptador `aws_rds`

### 4.1. Cuenta y usuario IAM

1. Tener una cuenta AWS.
2. Crear un **usuario IAM dedicado** (no usar la cuenta root) en la consola IAM.
3. Adjuntarle políticas:
   - `AmazonRDSFullAccess` (crear/listar/borrar instancias).
   - `AmazonEC2FullAccess` (la API crea/edita un Security Group para abrir la
     red automáticamente; con `AmazonEC2ReadOnlyAccess` la creación de la
     instancia funciona, pero la apertura de red fallará).
4. Generar **access keys** (Access Key ID + Secret) para ese usuario: pestaña
   *Security credentials* → *Create access key* → caso de uso **CLI**. El
   Secret **solo se muestra una vez**; descarga el `.csv`.

> La consola empuja hacia **IAM Identity Center** (SSO) en lugar de usuarios
> IAM con claves estáticas. Para este proyecto se usan claves estáticas a
> propósito: son las que consume `aws configure` y la cadena de credenciales
> por defecto de boto3, sin paso de renovación de sesión.

Verificación de los tres eslabones por separado (identidad, permisos de RDS y
permisos de EC2), que es lo que evita el diagnóstico caro descrito en §4.3:

```bash
aws sts get-caller-identity     # identidad; el ARN no debe acabar en :root
```
```python
from app.adapters.registry import get_adapter_class
print(get_adapter_class("aws_rds")().list_instances())   # permisos + regiones
```

### 4.2. Credenciales

`boto3` las lee de un fichero estándar cuya ubicación depende del sistema:

| Sistema | Ruta |
|---|---|
| Linux / macOS | `~/.aws/credentials` |
| Windows | `%USERPROFILE%\.aws\credentials` |

Lo más simple es generarlo con `aws configure`, que pregunta las 4 claves.
También se puede escribir a mano:

```ini
# credentials
[default]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
```
```ini
# config
[default]
region = eu-west-1
output = json
```

Verificar: `aws sts get-caller-identity` (o vía boto3).

> **Tras reinstalar el sistema operativo** hay que rehacer este paso. El
> Secret Access Key **no se puede recuperar** de la consola de AWS: si solo
> existía en la máquina anterior, hay que generar unas claves nuevas en IAM
> y desactivar las antiguas. El usuario IAM y sus políticas se conservan.

### 4.3. Conectarse a una instancia RDS (data plane) — AUTOMÁTICO

Desde jul 2026 el adaptador abre la red él solo al crear la instancia:

1. Marca la instancia como **públicamente accesible** (`PubliclyAccessible`).
2. Crea (una vez por región, y lo reutiliza) el Security Group
   `cloudapi-dbaccess` en la Default VPC, con una regla de entrada para el
   puerto del motor (5432/3306) **solo desde la IP pública de la máquina que
   ejecuta la API** (`/32`). La IP se detecta vía `checkip.amazonaws.com`.

Casos especiales:

- Si defines `AWS_SECURITY_GROUP_ID` en `.env`, se usa ese SG tal cual y la
  API no crea ni toca reglas (tú gestionas la red).
- Si la IP pública no se puede detectar (sin internet), la instancia se crea
  igual con la red cerrada y lo verás en el campo `note`. Apertura manual:
  ```bash
  aws ec2 authorize-security-group-ingress --region <region> \
    --group-id sg-XXXX --protocol tcp --port 5432 --cidr <TU_IP>/32
  ```
- El SG `cloudapi-dbaccess` no se borra al eliminar instancias (es gratis y
  reutilizable). Si quieres limpiarlo: `aws ec2 delete-security-group`.
- Si tu IP pública cambia (router reiniciado), crea una instancia nueva o
  añade la IP nueva al SG a mano; las reglas antiguas no se revocan solas.

---

## 5. Adaptador `gcp_cloudsql`

### 5.1. gcloud, proyecto, facturación, API

```bash
# 1. Instalar e iniciar sesión
sudo snap install google-cloud-cli --classic   # Windows: winget install Google.CloudSDK
gcloud auth login

# 2. Crear (o elegir) un proyecto y activarlo
gcloud projects create <PROJECT_ID> --name="cloud-db-api TFM"
gcloud config set project <PROJECT_ID>

# 3. Vincular una cuenta de facturación (necesario para Cloud SQL)
gcloud billing accounts list
gcloud billing projects link <PROJECT_ID> --billing-account=XXXXXX-XXXXXX-XXXXXX

# 4. Habilitar la Cloud SQL Admin API
gcloud services enable sqladmin.googleapis.com
```

### 5.2. Credenciales para el código (ADC)

```bash
gcloud auth application-default login
```

Crea el fichero de Application Default Credentials, que la librería de Google
lee automáticamente:

| Sistema | Ruta |
|---|---|
| Linux / macOS | `~/.config/gcloud/application_default_credentials.json` |
| Windows | `%APPDATA%\gcloud\application_default_credentials.json` |

**Marca todos los permisos** en la pantalla de consentimiento (incluido
`cloud-platform`) o fallará.

> Ojo: `gcloud auth login` (para el CLI) y `gcloud auth application-default
> login` (para el código) son **dos logins distintos**. La API usa el segundo.

Configurar el proyecto en `.env`:
```
PROVIDER=gcp_cloudsql
GCP_PROJECT=<PROJECT_ID>
GCP_REGION=europe-west1
```

### 5.3. Conectarse a una instancia Cloud SQL (data plane) — AUTOMÁTICO

Desde jul 2026 el adaptador añade la IP pública de la máquina que ejecuta la
API a las `authorizedNetworks` de la instancia al crearla (equivalente GCP
del Security Group, pero por instancia). No hay que hacer nada.

Fallback manual (solo si la detección de IP falló — lo verás en `note` —
o si tu IP cambió después de crear la instancia):

```bash
gcloud sql instances patch <INSTANCE_NAME> --authorized-networks=<TU_IP>/32
```

### 5.4. Notas de Cloud SQL

- El usuario administrador lo fija el motor (`postgres`/`root`); el adaptador
  crea además un usuario con tu `admin_username` de forma diferida.
- La instancia se crea en edición `ENTERPRISE` (la única que admite el tier
  barato `db-f1-micro`).

---

## 6. Comprobación rápida del data plane

Con una instancia ya creada (la red se abre sola al crearla):

```bash
uv run python scripts/demo_ingest.py --host <HOST> --port <PORT> \
    --user <USER> --dbname <DB>
```

El mismo script funciona contra Docker, AWS y GCP sin cambios: prueba de que
el plano de datos es agnóstico de proveedor.

---

## 7. Aviso de costes

- **Docker**: gratis.
- **AWS RDS**: **ya NO hay free tier clásico en cuentas nuevas.** Las cuentas
  creadas desde el cambio de modelo de AWS (jul 2025) reciben un plan de
  créditos con caducidad en lugar de los 12 meses con 750 h/mes de
  `db.t3.micro`. Verifica el régimen que te aplica en **Billing → Free tier**
  antes de crear nada: si es el nuevo, `db.t3.micro` **consume crédito desde
  la primera hora**.
- **GCP Cloud SQL**: NO tiene free tier permanente; usa los $300 de crédito
  de prueba. `db-f1-micro` cuesta si se deja encendida.

> Comprobado en septiembre de 2026 sobre una cuenta AWS recién creada: no
> aplica capa gratuita. Es decir, **los dos proveedores gestionados facturan**,
> así que la regla de abajo ya no es una precaución para GCP sino la única
> forma sostenible de operar contra cualquiera de los dos.

**Regla de oro:** las pruebas cloud deben ser efímeras (crear → probar →
borrar en minutos). Borrar siempre al terminar:

```bash
# Docker
just docker-clean
# AWS
aws rds delete-db-instance --region <region> --db-instance-identifier <id> \
  --skip-final-snapshot
# GCP
gcloud sql instances delete <instance-name>
```

---

## 8. Notas específicas de Windows

El proyecto se desarrolló en Ubuntu. Se ha verificado en Windows 11 que
**el código de la aplicación es portable sin cambios**: los 110 tests
rápidos y los 2 de integración con Docker real pasan, y el flujo completo
(crear instancia → consola SQL → borrar) funciona igual. Lo que sí cambia
es el entorno alrededor.

### 8.1. Qué funciona igual

Control plane con `local_docker`, `aws_rds` y `gcp_cloudsql`; data plane
(`/query`, `/data/*`); la UI web; y las recetas `just` de uso diario
(`test`, `run`, `lint`, `format`).

La detección de IP pública para la apertura automática de red
(`app/network.py`) usa `urllib` y es portable.

### 8.2. Qué hubo que adaptar

| Elemento | Motivo |
|---|---|
| `justfile` | En Windows `just` usa `cmd.exe`, que no entiende `$(...)`, `2>/dev/null` ni `rm`. Se fija PowerShell con `set windows-shell` y las recetas `clean`, `docker-clean` y `k8s-deploy` se duplican con los atributos `[unix]` / `[windows]`. |
| `.env` | `DOCKER_HOST` debe quedar sin definir (ver §3). |
| Rutas de credenciales | Distintas ubicación en Windows (ver §4.2 y §5.2). |

### 8.3. Limitación conocida: fase 4 (Kubernetes)

El despliegue en kind + Helm está diseñado sobre **semántica POSIX** y no se
ha validado en Windows:

- `deploy/kind-config.yaml` monta `/var/run/docker.sock` como `hostPath`.
- `deploy/helm/cloud-db-api/values.yaml` usa `dockerSocket.groupId` con
  `supplementalGroups`, un mecanismo de permisos de grupo de Linux que no
  tiene equivalente en Windows.
- La receta original calculaba el gid con `getent group docker`, comando
  inexistente en Windows.

La variante `[windows]` de `k8s-deploy` omite el `--set` del gid y usa el
valor por defecto del chart (el socket dentro de la VM Linux de Docker
Desktop). **Puede requerir ajustes manuales.** Si el objetivo es reproducir
la fase 4 tal cual se documentó, lo más fiable es ejecutarla desde WSL2 o
desde una máquina Linux.
