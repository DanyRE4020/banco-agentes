-- Migración idempotente. Ejecutar con el dueño de las tablas de referencia.
BEGIN;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'banco_cliente') THEN
    CREATE ROLE banco_cliente LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  ELSIF EXISTS (
    SELECT FROM pg_roles WHERE rolname = 'banco_cliente'
    AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
  ) OR EXISTS (SELECT FROM pg_auth_members WHERE member = 'banco_cliente'::regrole) THEN
    RAISE EXCEPTION 'banco_cliente debe ser un rol dedicado sin privilegios elevados ni membresías';
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO banco_cliente', current_database());
END
$$;

ALTER ROLE banco_cliente SET default_transaction_read_only = on;
REVOKE ALL ON SCHEMA dim, cfg FROM PUBLIC, banco_cliente;
REVOKE ALL ON ALL TABLES IN SCHEMA dim, cfg FROM banco_cliente;
CREATE SCHEMA IF NOT EXISTS cliente;
REVOKE ALL ON SCHEMA cliente FROM PUBLIC, banco_cliente;
GRANT USAGE ON SCHEMA cliente TO banco_cliente;

CREATE OR REPLACE VIEW cliente.saldos WITH (security_barrier = true) AS
SELECT num_cte, num_cta, cve_producto, fec_informacion, saldo_mes, saldo_apertura
FROM cfg.fact_sdos_captacoloca_m
WHERE num_cte = current_setting('app.num_cte')::integer;

CREATE OR REPLACE VIEW cliente.movimientos WITH (security_barrier = true) AS
SELECT num_cte, num_cta, cve_producto, fec_txn, cve_mes_txn, cve_txn, saldo_txn
FROM cfg.fact_txn_captacoloca_d
WHERE num_cte = current_setting('app.num_cte')::integer;

REVOKE ALL ON cliente.saldos, cliente.movimientos FROM PUBLIC, banco_cliente;
GRANT SELECT ON cliente.saldos, cliente.movimientos TO banco_cliente;
COMMIT;
