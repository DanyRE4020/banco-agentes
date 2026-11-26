SELECT SUM(saldo_mes) AS total, COUNT(*) AS filas,
       COUNT(saldo_mes) AS saldos_presentes, COUNT(DISTINCT num_cta) AS cuentas
FROM cfg.fact_sdos_captacoloca_m
WHERE cve_producto = %s AND fec_informacion = %s
