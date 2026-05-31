"""
Contrato que todo adaptador de proveedor debe implementar.

Esta clase es el corazón del patrón Adapter/Strategy del TFM. La API REST
solo conoce esta interfaz; nunca habla con boto3, ni con docker, ni con el
SDK de GCP directamente. Eso es lo que permite que cambiar de proveedor sea
una decisión de configuración, no de código.

Reglas para nuevos métodos:
- Antes de añadir un método aquí, asegúrate de que tiene sentido para TODOS
  los proveedores razonables (Docker, AWS, GCP, Azure, on-prem manual).
- Si una operación es muy específica de un proveedor, NO va aquí: va como
  endpoint específico o como extensión documentada.
"""

from abc import ABC, abstractmethod

from app.models import InstanceCreateRequest, InstanceInfo


class DatabaseAdapter(ABC):
    """Interfaz unificada de gestión de instancias de BBDD relacionales."""

    @abstractmethod
    def create_instance(self, request: InstanceCreateRequest) -> InstanceInfo:
        """Crear una nueva instancia. Devuelve los datos de conexión."""
        ...

    @abstractmethod
    def get_instance(self, instance_id: str) -> InstanceInfo:
        """Recuperar el estado actual de una instancia por id."""
        ...

    @abstractmethod
    def list_instances(self) -> list[InstanceInfo]:
        """Listar todas las instancias gestionadas por esta API."""
        ...

    @abstractmethod
    def delete_instance(self, instance_id: str) -> None:
        """Eliminar una instancia. Idempotente: borrar algo que no existe no falla."""
        ...
