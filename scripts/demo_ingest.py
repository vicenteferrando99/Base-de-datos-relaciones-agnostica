"""
Demo de ingesta en el *data plane*.

Se conecta a una instancia YA aprovisionada (por cualquier adaptador:
local_docker, aws_rds, gcp_cloudsql) y ejecuta el MISMO SQL en todos los
casos. Es la demostración práctica de que, una vez creada la instancia, el
plano de datos es agnóstico de proveedor: un cliente PostgreSQL estándar
habla igual con Docker, AWS RDS o GCP Cloud SQL. La API resuelve la
dependencia de proveedor en el *control plane*; el *data plane* ya es
portable por sí mismo.

La contraseña se pide de forma interactiva (no se pasa por CLI ni queda en
logs ni en el historial del shell).

Uso:
    uv run python scripts/demo_ingest.py --host <HOST> --port <PORT> \
        --user <USER> --dbname <DB>

Ejemplos:
    # GCP Cloud SQL (usuario fijo 'postgres', db por defecto 'postgres')
    uv run python scripts/demo_ingest.py --host 34.x.x.x --user postgres --dbname postgres

    # Docker local (usuario y db = los que pediste al crear)
    uv run python scripts/demo_ingest.py --host localhost --port 54321 \
        --user dbadmin --dbname ventas
"""

import argparse
import getpass

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--dbname", default="postgres")
    args = parser.parse_args()

    password = getpass.getpass(f"Contraseña de {args.user}@{args.host}: ")

    with (
        psycopg.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=password,
            dbname=args.dbname,
            connect_timeout=10,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS operaciones (
                id SERIAL PRIMARY KEY,
                cliente TEXT NOT NULL,
                importe NUMERIC(12, 2) NOT NULL,
                alta DATE DEFAULT CURRENT_DATE
            )
            """
        )
        cur.executemany(
            "INSERT INTO operaciones (cliente, importe) VALUES (%s, %s)",
            [("Ana", 1500.00), ("Bruno", 2750.50), ("Carla", 990.00)],
        )
        conn.commit()
        cur.execute("SELECT id, cliente, importe, alta FROM operaciones ORDER BY id")
        rows = cur.fetchall()

    print(f"\n✅ Conexión OK a {args.host}:{args.port}/{args.dbname}")
    print(f"Filas en 'operaciones' ({len(rows)}):")
    for r in rows:
        print("  ", r)


if __name__ == "__main__":
    main()
