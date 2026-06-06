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
    # Si es None, el SDK aplica su lógica por defecto (variable de entorno
    # DOCKER_HOST o `/var/run/docker.sock`). Hay que fijarlo cuando se usa
    # Docker Desktop en Linux: su socket vive en `~/.docker/desktop/docker.sock`
    # y no en la ruta clásica.
    docker_host: str | None = None

    # --- AWS (solo usado por aws_rds) ---
    # Las credenciales NO viven aquí; boto3 las lee de ~/.aws/credentials.
    # Aquí solo región y, opcionalmente, IDs de red para overridear la
    # Default VPC.
    aws_region: str = "eu-west-1"
    aws_db_subnet_group: str | None = None
    aws_security_group_id: str | None = None


settings = Settings()
