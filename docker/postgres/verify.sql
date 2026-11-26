\set ON_ERROR_STOP on
SELECT current_database() AS base, current_user AS usuario,
       current_setting('default_transaction_read_only') AS solo_lectura_por_defecto;

SELECT 'dim.dim_cliente' AS tabla, count(*) AS filas FROM dim.dim_cliente
UNION ALL SELECT 'dim.dim_producto', count(*) FROM dim.dim_producto
UNION ALL SELECT 'dim.dim_txn', count(*) FROM dim.dim_txn
UNION ALL SELECT 'cfg.fact_sdos_captacoloca_m', count(*) FROM cfg.fact_sdos_captacoloca_m
UNION ALL SELECT 'cfg.fact_txn_captacoloca_d', count(*) FROM cfg.fact_txn_captacoloca_d;

-- Ejemplo determinista: cuatro productos, 5.000 cuentas/clientes en junio.
SELECT p.cve_producto, count(*) AS cuentas, count(DISTINCT s.num_cte) AS clientes
FROM dim.dim_producto p
JOIN cfg.fact_sdos_captacoloca_m s USING (cve_producto)
WHERE s.fec_informacion = 20260630
GROUP BY p.cve_producto ORDER BY p.cve_producto;

DO $$
DECLARE t record;
BEGIN
  IF (SELECT count(*) FROM dim.dim_cliente) <> 5000
     OR (SELECT count(DISTINCT num_cte) FROM dim.dim_cliente) <> 5000
     OR (SELECT count(*) FROM dim.dim_cliente WHERE tipo_persona='PF') <> 4800
     OR (SELECT count(*) FROM dim.dim_cliente WHERE tipo_persona='PM') <> 200
     OR (SELECT count(*) FROM dim.dim_producto) <> 4
     OR (SELECT count(*) FROM dim.dim_txn) <> 9
     OR (SELECT count(*) FROM cfg.fact_sdos_captacoloca_m) <> 120000
     OR (SELECT count(*) FROM cfg.fact_txn_captacoloca_d) <> 225000 THEN
    RAISE EXCEPTION 'Conteos distintos de los esperados en los SQL originales';
  END IF;
  IF (SELECT count(*) FROM pg_tables WHERE schemaname IN ('dim','cfg')) <> 5
     OR to_regclass('cfg.tmp_calculo_mes') IS NOT NULL THEN
    RAISE EXCEPTION 'Se esperaban cinco tablas permanentes, sin la temporal';
  END IF;
  IF (SELECT count(DISTINCT fec_informacion) FROM cfg.fact_sdos_captacoloca_m) <> 6
     OR EXISTS (SELECT 1 FROM cfg.fact_sdos_captacoloca_m
                GROUP BY fec_informacion, cve_producto
                HAVING count(*) <> 5000 OR count(DISTINCT num_cte) <> 5000)
     OR (SELECT count(*) FROM cfg.fact_sdos_captacoloca_m WHERE fec_informacion=20260630) <> 20000
     OR (SELECT count(DISTINCT cve_mes_txn) FROM cfg.fact_txn_captacoloca_d) <> 5
     OR EXISTS (SELECT 1 FROM cfg.fact_txn_captacoloca_d
                GROUP BY cve_mes_txn HAVING count(*) <> 45000)
     OR EXISTS (SELECT 1 FROM cfg.fact_txn_captacoloca_d WHERE saldo_txn IS NULL OR saldo_txn <= 0) THEN
    RAISE EXCEPTION 'Distribución mensual o importes inesperados';
  END IF;
  IF current_setting('default_transaction_read_only') <> 'on'
     OR has_database_privilege(current_user, current_database(), 'CREATE')
     OR has_database_privilege(current_user, current_database(), 'TEMP')
     OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname=current_user
                AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
    RAISE EXCEPTION 'El rol de aplicación tiene capacidades excesivas';
  END IF;
  FOR t IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('dim','cfg') LOOP
    IF NOT has_table_privilege(current_user, format('%I.%I',t.schemaname,t.tablename), 'SELECT')
       OR has_table_privilege(current_user, format('%I.%I',t.schemaname,t.tablename), 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_schema_privilege(current_user, t.schemaname, 'CREATE') THEN
      RAISE EXCEPTION 'Privilegios incorrectos en %.%', t.schemaname, t.tablename;
    END IF;
  END LOOP;
  RAISE NOTICE 'OK: conteos, distribución, consulta de ejemplo y privilegios';
END $$;

-- Prueba de permisos independiente del modo read-only; WHERE false no modifica
-- ninguna fila incluso si una configuración errónea concediera UPDATE.
BEGIN;
SET LOCAL transaction_read_only = off;
DO $$
BEGIN
  BEGIN
    UPDATE dim.dim_cliente SET num_cte=num_cte WHERE false;
    RAISE EXCEPTION 'ERROR: se permitió UPDATE al usuario de lectura';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK: UPDATE denegado por privilegios';
  END;
END $$;
ROLLBACK;
