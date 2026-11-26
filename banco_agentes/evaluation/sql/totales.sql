SELECT f.cve_producto, f.fec_informacion / 100 AS mes,
       sum(f.saldo_mes), count(*), count(f.saldo_mes)
FROM cfg.fact_sdos_captacoloca_m AS f
JOIN dim.dim_cliente AS c ON c.num_cte = f.num_cte
WHERE f.cve_producto = ANY(%s) AND f.fec_informacion = ANY(%s)
  AND (%s = '' OR c.tipo_persona = %s)
GROUP BY f.cve_producto, mes ORDER BY f.cve_producto, mes
