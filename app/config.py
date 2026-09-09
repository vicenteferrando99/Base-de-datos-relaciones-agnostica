"""
Configuración de la aplicación.

El campo CLAVE aquí es `provider`: determina qué adaptador se usa en runtime.
Cambiando esta variable de entorno (o el .env) se cambia el backend completo
sin tocar una línea de código de negocio. Esa es la tesis del TFM.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración leída de variables de entorno y/o .env"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Adaptador activo: "local_docker" | "aws_rds" | "gcp_cloudsql" (futuros)
    provider: str = "local_docker"

    # API
    api_title: str = "Cloud DB API"
    api_version: str = "0.1.0"

    # URL del socket del daemon de Docker. Solo lo usa `local_docker`.
    # Si es None, el SDK aplica su lógica por defecto, que acierta en casi
    # todos los entornos: `/var/run/docker.sock` en Linux con Docker Engine,
    # el named pipe `npipe:////./pipe/docker_engine` en Windows con Docker
    # Desktop, y `~/.docker/run/docker.sock` en macOS.
    #
    # El único caso que exige fijarlo a mano es Docker Desktop EN LINUX, cuyo
    # socket vive en `~/.docker/desktop/docker.sock` y no en la ruta clásica.
    #
    # Cuidado al migrar de sistema: un valor `unix://...` heredado de un .env
    # de Linux rompe el adaptador en Windows. En Windows, dejarlo sin definir.
    docker_host: str | None = None

    # Host que se anuncia en los datos de conexión de las instancias Docker.
    # "localhost" vale cuando la API corre en la misma máquina que el daemon.
    #
    # Si la propia API se ejecuta DENTRO de un contenedor, el daemon sigue
    # siendo el del anfitrión pero "localhost" pasa a ser el contenedor mismo,
    # de modo que los datos de conexión que se devuelven al cliente dejan de
    # ser alcanzables. En ese caso hay que poner aquí la dirección por la que
    # el contenedor llega al anfitrión (la puerta de enlace de su red, p. ej.
    # 172.17.0.1 en la red bridge por defecto de Docker).
    docker_instance_host: str = "localhost"

    # --- AWS (solo usado por aws_rds) ---
    # Las credenciales NO viven aquí; boto3 las lee de ~/.aws/credentials.
    # Aquí solo región y, opcionalmente, IDs de red para overridear la
    # Default VPC.
    aws_region: str = "eu-west-1"
    aws_db_subnet_group: str | None = None
    aws_security_group_id: str | None = None

    # --- GCP (solo usado por gcp_cloudsql) ---
    # Las credenciales NO viven aquí; la librería de Google las lee de las
    # Application Default Credentials (`gcloud auth application-default login`,
    # fichero ~/.config/gcloud/application_default_credentials.json).
    # `gcp_project` es obligatorio si PROVIDER=gcp_cloudsql.
    gcp_project: str | None = None
    gcp_region: str = "europe-west1"


settings = Settings()
