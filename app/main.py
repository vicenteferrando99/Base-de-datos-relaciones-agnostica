"""
Punto de entrada de la API REST — CONTROL PLANE.

Aquí viven la `app` de FastAPI y los endpoints de gestión de instancias.
Este fichero solo conoce el contrato abstracto `DatabaseAdapter`, nunca los
SDKs nativos: esa frontera materializa la tesis del TFM (cambiar de
proveedor = cambiar configuración, no código).

El DATA PLANE (SQL libre y operaciones agnósticas de motor) vive en
`app/dataops/` y se monta aquí como router. Ver avisos de seguridad allí.
"""

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.adapters.base import DatabaseAdapter
from app.adapters.registry import available_providers, get_adapter_class
from app.config import settings
from app.dataops.api import router as dataops_router
from app.models import InstanceCreateRequest, InstanceInfo

app = FastAPI(title=settings.api_title, version=settings.api_version)
app.include_router(dataops_router)

_UI_HTML_PATH = Path(__file__).parent.parent / "ui" / "index.html"

# Proveedor activo. Se inicializa con el de la configuración (.env) pero puede
# cambiarse en caliente vía PUT /provider (lo usa el selector de la UI).
# NOTA: es estado de proceso. Con un único worker (desarrollo) es correcto;
# con varios workers cada uno tendría su propio valor. Aceptable para el TFM.
_active_provider = settings.provider

# Caché de adaptadores ya instanciados, por nombre de proveedor. Evita
# reconstruir el cliente (boto3/discovery/docker) en cada petición.
_adapter_cache: dict[str, DatabaseAdapter] = {}


def _adapter_for(provider: str) -> DatabaseAdapter:
    """Instancia (o reutiliza del caché) el adaptador de `provider`."""
    if provider not in _adapter_cache:
        _adapter_cache[provider] = get_adapter_class(provider)()
    return _adapter_cache[provider]


def get_adapter() -> DatabaseAdapter:
    """Adaptador del proveedor activo. Lo inyecta FastAPI como dependencia."""
    return _adapter_for(_active_provider)


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
    provider: str | None = None,
    adapter: DatabaseAdapter = Depends(get_adapter),
) -> list[InstanceInfo]:
    """Lista las instancias gestionadas.

    Por defecto, las del proveedor activo. Con `?provider=` permite consultar
    explícitamente otro proveedor sin cambiar el activo; la interfaz web mantiene
    su workspace sincronizado con el proveedor global.
    """
    if provider is not None:
        if provider not in available_providers():
            raise HTTPException(status_code=400, detail=f"Proveedor no soportado: {provider!r}")
        adapter = _adapter_for(provider)
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


@app.get("/health", include_in_schema=False)
def health() -> dict:
    """Meta-info para la UI: proveedor activo y proveedores disponibles."""
    return {
        "provider": _active_provider,
        "available_providers": available_providers(),
        "api_version": settings.api_version,
    }


class ProviderChange(BaseModel):
    provider: str


@app.put("/provider", include_in_schema=False)
def set_provider(change: ProviderChange) -> dict:
    """Cambia el proveedor activo en caliente (lo usa el selector de la UI).

    Solo valida que el proveedor exista en el registry. Si sus credenciales
    no están configuradas, el error aparecerá al usarlo (no aquí). Esto
    materializa la tesis del TFM: cambiar de proveedor es cambiar un valor,
    sin tocar el código de negocio.
    """
    global _active_provider
    if change.provider not in available_providers():
        raise HTTPException(
            status_code=400,
            detail=f"Proveedor no soportado: {change.provider!r}. "
            f"Disponibles: {available_providers()}",
        )
    _active_provider = change.provider
    return {"provider": _active_provider}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui() -> str:
    """Sirve la mini-UI web (Tailwind + Alpine.js)."""
    return _UI_HTML_PATH.read_text(encoding="utf-8")
