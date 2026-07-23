"""
Utilidades de red compartidas por los adaptadores cloud.

Contexto: las instancias cloud (RDS, Cloud SQL) nacen con la red CERRADA.
Para que el data plane de esta API (consola SQL, /data/*) pueda conectarse,
hay que autorizar la IP pública DE LA MÁQUINA QUE EJECUTA LA API — es el
proceso de la API, no el navegador, quien abre la conexión con la BBDD.

`get_public_ip()` la descubre preguntando a un servicio de eco de IP
(checkip.amazonaws.com, el mismo que usa la consola de AWS). Se cachea el
primer resultado válido: la IP pública no cambia durante una sesión de
trabajo y así solo se paga una petición HTTP por proceso.

Si la detección falla (sin internet, servicio caído) devuelve None y los
adaptadores degradan con elegancia: crean la instancia igualmente, con la
red cerrada, y lo avisan en el campo `note` (mismo comportamiento manual
que había antes de automatizar esto).
"""

import ipaddress
import logging
import urllib.request

logger = logging.getLogger(__name__)

_ECHO_URL = "https://checkip.amazonaws.com"
_TIMEOUT_S = 5

# Caché de proceso: solo se guarda un resultado VÁLIDO (los fallos se
# reintentan en la siguiente llamada).
_cached_ip: str | None = None


def get_public_ip() -> str | None:
    """IP pública de esta máquina, o None si no se pudo detectar."""
    global _cached_ip
    if _cached_ip is not None:
        return _cached_ip
    try:
        with urllib.request.urlopen(_ECHO_URL, timeout=_TIMEOUT_S) as response:
            candidate = response.read().decode().strip()
        ipaddress.ip_address(candidate)  # valida el formato; lanza si no es IP
    except Exception:
        logger.warning("No se pudo detectar la IP pública (¿sin internet?)")
        return None
    _cached_ip = candidate
    return _cached_ip
