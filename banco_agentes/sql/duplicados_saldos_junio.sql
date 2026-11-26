WITH scoped AS (
    SELECT num_cta, cve_producto, fec_informacion
    FROM cfg.fact_sdos_captacoloca_m
    WHERE fec_informacion = %s
      AND (%s::varchar IS NULL OR cve_producto = %s)
), duplicates AS (
    SELECT num_cta, cve_producto, fec_informacion, COUNT(*) AS occurrences
    FROM scoped
    GROUP BY num_cta, cve_producto, fec_informacion
    HAVING COUNT(*) > 1
)
SELECT (SELECT COUNT(*) FROM scoped) AS checked_rows,
       COUNT(*) AS duplicate_groups,
       COALESCE(SUM(occurrences), 0) AS duplicate_rows,
       COALESCE(SUM(occurrences - 1), 0) AS excess_rows,
       (SELECT COUNT(*) FROM scoped WHERE num_cta IS NULL OR cve_producto IS NULL) AS null_key_rows
FROM duplicates
