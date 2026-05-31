"""
Punto de entrada de la API REST.

Aquí viven la `app` de FastAPI, los endpoints HTTP y la función helper que
elige qué adaptador usar según `settings.provider`. La regla de oro: este
fichero NO importa SDKs nativos (docker, boto3...), solo el contrato
abstracto `DatabaseAdapter`. Esa es la frontera que materializa la tesis
del TFM: cambiar de proveedor = cambiar configuración, no código.
"""

from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException

from app.adapters.base import DatabaseAdapter
from app.adapters.local_docker import LocalDockerAdapter
from app.config import settings
from app.models import InstanceCreateRequest, InstanceInfo

app = FastAPI(title=settings.api_title, version=settings.api_version)


@lru_cache
def get_adapter() -> DatabaseAdapter:
    """Devuelve el adaptador activo según `settings.provider`.

    Mientras solo haya un proveedor implementado, una rama `if` simple
    basta. Cuando entren 2-3 adaptadores conviene refactorizar a un
    registry/factoría (ver fase posterior en CLAUDE.md).
    """
    if settings.provider == "local_docker":
        return LocalDockerAdapter()
    raise RuntimeError(f"Proveedor no soportado: {settings.provider!r}")


@app.post("/instances", response_model=InstanceInfo, status_code=201)
def create_instance(
    request: InstanceCreateRequest,
    adapter: DatabaseAdapter = Depends(get_adapter),
) -> InstanceInfo:
    """Crea una nueva instancia de BBDD en el proveedor activo."""
    try:
        return adapter.create_instance(request)
    except Exception as exc:  # mapeo fino de errores: trabajo futuro
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/instances", response_model=list[InstanceInfo])
def list_instances(
    adapter: DatabaseAdapter = Depends(get_adapter),
) -> list[InstanceInfo]:
    """Lista todas las instancias gestionadas por la API en el proveedor activo."""
    try:
        return adapter.list_instances()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/instances/{instance_id}", response_model=InstanceInfo)
def get_instance(
    instance_id: str,
    adapter: DatabaseAdapter = Depends(get_adapter),
) -> InstanceInfo:
    """Devuelve los datos de una instancia por id. 404 si no existe."""
    try:
        return adapter.get_instance(instance_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/instances/{instance_id}", status_code=204)
def delete_instance(
    instance_id: str,
    adapter: DatabaseAdapter = Depends(get_adapter),
) -> None:
    """Elimina una instancia por id. Idempotente: 204 también si no existía."""
    try:
        adapter.delete_instance(instance_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
