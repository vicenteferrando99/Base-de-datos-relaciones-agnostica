"""
Capa de operaciones sobre datos agnóstica de MOTOR (data plane).

Mientras `app/adapters/` abstrae el PROVEEDOR (AWS vs GCP vs Docker) en el
control plane, este paquete abstrae el MOTOR (PostgreSQL vs MySQL) en el
data plane: el cliente describe la operación (crear tabla, insertar,
consultar) en un modelo abstracto y cada dialecto genera el SQL nativo.

Es el mismo patrón Adapter aplicado a la otra dimensión del problema.

Acotación deliberada (ver docs/DATA_PLANE.md §1): el conjunto de operaciones
es CERRADO. NO se parsea ni se traduce SQL arbitrario escrito por el usuario;
eso (dialectos completos, AST) queda como trabajo futuro.
"""
