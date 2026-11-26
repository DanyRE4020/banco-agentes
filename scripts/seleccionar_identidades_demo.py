"""Selección real por SQL; no inventa titulares ni modifica datos."""
import json
from banco_agentes.analytics import connect_readonly
from banco_agentes.catalog import ROOT

with connect_readonly() as conn:
    rows = conn.execute("""SELECT num_cte, MIN(num_cta) FROM cfg.fact_sdos_captacoloca_m
        WHERE cve_producto=%s AND fec_informacion=%s AND saldo_mes IS NOT NULL
        GROUP BY num_cte ORDER BY num_cte LIMIT 2""", ("AHORRO",20260630)).fetchall()
assert len(rows) == 2 and rows[0][0] != rows[1][0]
result = dict(client=dict(num_cte=rows[0][0], num_cta=rows[0][1]),
              other=dict(num_cte=rows[1][0], num_cta=rows[1][1]),
              source="SQL sobre cfg.fact_sdos_captacoloca_m, AHORRO, 20260630")
(ROOT/"config").mkdir(exist_ok=True)
(ROOT/"config/demo_sessions.json").write_text(json.dumps(result, indent=2)+"\n",encoding="utf-8")
print(json.dumps(result))
