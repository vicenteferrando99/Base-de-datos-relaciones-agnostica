"""
Registry de adaptadores disponibles.

Cada adaptador se da de alta DECORANDO su clase con `@register("nombre")`.
La API consulta al registry por el adaptador activo según `settings.provider`.

Ventaja: `app/main.py` no necesita conocer los adaptadores concretos.
Añadir un proveedor nuevo se reduce a crear su fichero, decorar la clase y
asegurarse de que el módulo se importa al cargar `app/adapters/__init__.py`.
Ningún cambio en `main.py`, ningún `if/elif` que crece con el tiempo.

Esta es una aplicación del Principio de Inversión de Dependencias (DIP):
el módulo de alto nivel (`main.py`) depende de una abstracción (el registry),
no de implementaciones concretas (`LocalDockerAdapter`, `AwsRdsAdapter`...).
"""

from collections.abc import Callable

from app.adapters.base import DatabaseAdapter

_REGISTRY: dict[str, type[DatabaseAdapter]] = {}


def register(
    name: str,
) -> Callable[[type[DatabaseAdapter]], type[DatabaseAdapter]]:
    """Decorador: da de alta una clase adaptadora bajo el nombre `name`."""

    def decorator(cls: type[DatabaseAdapter]) -> type[DatabaseAdapter]:
        if name in _REGISTRY:
            raise RuntimeError(
                f"Adaptador duplicado: {name!r} ya está registrado como {_REGISTRY[name].__name__}"
            )
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_adapter_class(name: str) -> type[DatabaseAdapter]:
    """Devuelve la clase adaptadora registrada bajo `name`."""
    if name not in _REGISTRY:
        raise ValueError(f"Proveedor no soportado: {name!r}. Disponibles: {available_providers()}")
    return _REGISTRY[name]


def available_providers() -> list[str]:
    """Nombres de adaptadores actualmente registrados. Útil en mensajes."""
    return sorted(_REGISTRY)
