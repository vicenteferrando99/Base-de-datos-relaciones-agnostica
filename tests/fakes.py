"""
Implementación en memoria del contrato `DatabaseAdapter`, para tests rápidos.

Este fichero tiene además un valor didáctico: demuestra (y los tests lo
verifican) que el patrón Adapter cumple su promesa. Basta con cumplir el
contrato `DatabaseAdapter` para que la API REST y el resto del sistema
funcionen sin tocar una sola línea de su código. Si esto es cierto para
un adaptador "fake" en memoria, también lo será para el adaptador AWS RDS,
GCP Cloud SQL o cualquiera futuro.
"""

import uuid

from app.adapters.base import DatabaseAdapter
from app.models import InstanceCreateRequest, InstanceInfo, InstanceStatus


class FakeAdapter(DatabaseAdapter):
    """Adaptador que simula el ciclo de vida sin Docker, AWS, ni red alguna."""

    PROVIDER_NAME = "fake"

    def __init__(self) -> None:
        self._instances: dict[str, InstanceInfo] = {}

    def create_instance(self, request: InstanceCreateRequest) -> InstanceInfo:
        instance_id = uuid.uuid4().hex[:8]
        info = InstanceInfo(
            id=instance_id,
            name=request.name,
            engine=request.engine,
            engine_version=request.engine_version or "fake-default",
            status=InstanceStatus.AVAILABLE,
            host="fake-host",
            port=5432,
            provider=self.PROVIDER_NAME,
        )
        self._instances[instance_id] = info
        return info

    def get_instance(self, instance_id: str) -> InstanceInfo:
        if instance_id not in self._instances:
            raise LookupError(f"Instancia no encontrada: {instance_id}")
        return self._instances[instance_id]

    def list_instances(self) -> list[InstanceInfo]:
        return list(self._instances.values())

    def delete_instance(self, instance_id: str) -> None:
        # `pop` con default => idempotente: borrar algo inexistente no falla.
        self._instances.pop(instance_id, None)
