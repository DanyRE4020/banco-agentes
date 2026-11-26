SELECT c.segmento_cte,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f.saldo_mes),
       stddev_samp(f.saldo_mes)
FROM cfg.fact_sdos_captacoloca_m AS f
JOIN dim.dim_cliente AS c ON c.num_cte = f.num_cte
WHERE f.cve_producto = %s AND f.fec_informacion = %s
GROUP BY c.segmento_cte ORDER BY c.segmento_cte
