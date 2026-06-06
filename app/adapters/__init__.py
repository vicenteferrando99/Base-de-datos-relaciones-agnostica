"""
Importar aquí cada módulo de adaptador para que su decorador `@register(...)`
se ejecute al cargar el paquete y la clase quede dada de alta en el registry.

Sin estos imports, Python no cargaría los módulos hasta que alguien los
importase explícitamente y `get_adapter_class("local_docker")` fallaría.
"""

from app.adapters import (
    aws_rds,  # noqa: F401  # activa @register(...)
    local_docker,  # noqa: F401  # activa @register(...)
)
