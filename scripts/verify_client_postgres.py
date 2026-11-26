"""Verifica la migración de Cliente en una base PostgreSQL local de pruebas vacía."""
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import InsufficientPrivilege, UndefinedObject

ROOT = Path(__file__).resolve().parents[1]


def main():
    dsn = os.getenv("BANCO_CLIENT_TEST_DSN")
    if not dsn:
        raise SystemExit("Falta BANCO_CLIENT_TEST_DSN con la conexión de administrador a una base vacía de pruebas.")
    settings = conninfo_to_dict(dsn)
    if settings.get("host") not in {"127.0.0.1", "localhost", "::1"} or not settings.get("dbname", "").startswith("banco_aislamiento_"):
        raise SystemExit("Usa una base local vacía con nombre banco_aislamiento_*.")

    with psycopg.connect(dsn) as db:
        try:
            if db.execute("""
                SELECT EXISTS (SELECT FROM pg_roles WHERE rolname='banco_cliente') OR
                       EXISTS (SELECT FROM pg_namespace WHERE nspname IN ('dim','cfg','cliente'))
            """).fetchone()[0]:
                raise SystemExit("La instancia de prueba ya tiene el rol o esquemas de Banco Agentes.")
            db.execute("""
                CREATE SCHEMA dim;
                CREATE SCHEMA cfg;
                CREATE TABLE cfg.fact_sdos_captacoloca_m (
                    num_cte integer, num_cta integer, cve_producto text,
                    fec_informacion integer, saldo_mes numeric, saldo_apertura numeric);
                CREATE TABLE cfg.fact_txn_captacoloca_d (
                    num_cte integer, num_cta integer, cve_producto text,
                    fec_txn integer, cve_mes_txn integer, cve_txn integer, saldo_txn numeric);
                INSERT INTO cfg.fact_sdos_captacoloca_m VALUES
                    (101, 1101, 'AHORRO', 20260630, 100, 95),
                    (101, 1102, 'TDC', 20260630, 20, 15),
                    (202, 2201, 'CHEQUES', 20260630, 200, 180),
                    (202, 2202, 'TDC', 20260630, 70, 60);
                INSERT INTO cfg.fact_txn_captacoloca_d VALUES
                    (101, 1102, 'TDC', 20260228, 202602, 400, 25),
                    (101, 1102, 'TDC', 20260228, 202602, 300, 50),
                    (202, 2202, 'TDC', 20260228, 202602, 400, 75);
            """)
            migration = (ROOT / "docker/postgres/client_access.sql").read_text(encoding="utf8")
            db.execute(migration.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0])
            db.execute("SET LOCAL ROLE banco_cliente")

            def denied(statement, expected):
                try:
                    with db.transaction():
                        db.execute(statement).fetchall()
                except expected:
                    return
                raise AssertionError("PostgreSQL no rechazó la consulta.")

            denied("SELECT * FROM cliente.saldos", UndefinedObject)
            denied("SELECT * FROM cfg.fact_sdos_captacoloca_m", InsufficientPrivilege)
            denied("SELECT * FROM cfg.fact_txn_captacoloca_d", InsufficientPrivilege)
            denied("DELETE FROM cliente.saldos RETURNING num_cta", InsufficientPrivilege)
            for identity, other, expected_accounts, expected_total in ((101,202,[1101,1102],25),(202,101,[2201,2202],75)):
                db.execute("SELECT set_config('app.num_cte', %s, true)", (str(identity),))
                accounts = db.execute("SELECT num_cta FROM cliente.saldos ORDER BY num_cta").fetchall()
                assert [row[0] for row in accounts] == expected_accounts
                amount = db.execute("SELECT sum(saldo_txn) FROM cliente.movimientos WHERE cve_producto='TDC' AND cve_txn=400").fetchone()[0]
                assert amount == expected_total
                for relation in ("cliente.saldos", "cliente.movimientos"):
                    assert db.execute(f"SELECT count(*) FROM {relation} WHERE num_cte=%s", (other,)).fetchone()[0] == 0
            print("PostgreSQL: 4 lecturas autorizadas, 8 controles negativos y 0 fugas.")
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
