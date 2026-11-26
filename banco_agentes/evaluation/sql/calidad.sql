WITH base AS (
    SELECT num_cta, cve_producto, fec_informacion, saldo_mes
    FROM cfg.fact_sdos_captacoloca_m
    WHERE cve_producto = %s AND fec_informacion = ANY(%s)
), grupos AS (
    SELECT num_cta, cve_producto, fec_informacion, count(*) AS n
    FROM base GROUP BY num_cta, cve_producto, fec_informacion HAVING count(*) > 1
), repetidos AS (
    SELECT fec_informacion, count(*) AS grupos_duplicados FROM grupos GROUP BY fec_informacion
)
SELECT b.fec_informacion / 100 AS mes, count(*) FILTER (WHERE b.saldo_mes IS NULL),
       coalesce(max(r.grupos_duplicados), 0), count(*)
FROM base AS b LEFT JOIN repetidos AS r ON r.fec_informacion = b.fec_informacion
GROUP BY b.fec_informacion ORDER BY b.fec_informacion
