"""Compara los planes de desarrollo con SQL directo de solo lectura."""
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from banco_agentes.analytics import connect_readonly
from banco_agentes.charts import chart_for
from banco_agentes.governance import SESSION, load_session
from banco_agentes.plans import execute_plan

BAL = "cfg.fact_sdos_captacoloca_m"
MOV = "cfg.fact_txn_captacoloca_d"
WHERE = f"FROM {BAL} WHERE cve_producto='CHEQUES' AND fec_informacion=20260331"
PF = f"FROM {BAL} b JOIN dim.dim_cliente c USING(num_cte) WHERE c.tipo_persona='PF' AND b.cve_producto='CHEQUES'"
DEFAULT = dict(conjunto="saldos", desde="2026-03", hasta="", producto="CHEQUES", persona="",
               segmento="", agrupar="", alcance="global", codigo=0, campo="", minimo=None, maximo=None)


def main():
    started = time.perf_counter()
    report = {"passed": False, "cases": [], "charts": []}
    try:
        with connect_readonly() as conn:
            user, read_only = conn.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
            report.update(user=user, read_only=read_only)
            assert (user, read_only) == ("banco_lectura", "on")

            def compare(metric, sql, fields, *, label=None, quality=False, graph=None, params=(), **scope):
                start = time.perf_counter()
                result = execute_plan({**DEFAULT, **scope, "metrica": metric}, quality=quality)
                assert result["status"] == "ok", result
                actual = [tuple(row[k] for k in fields) for row in result["rows"]]
                expected = conn.execute(sql, params).fetchall()
                assert len(actual) == len(expected), (actual, expected)
                for row, target in zip(actual, expected):
                    assert [Decimal(str(x)) for x in row] == [Decimal(str(x)) for x in target], (row, target)
                report["cases"].append(dict(case=label or metric, values=actual,
                                            seconds=time.perf_counter()-start, sql_seconds=result["seconds"]))
                if graph:
                    report["charts"].append(chart_for(result, graph))

            compare("total", f"SELECT sum(saldo_mes),count(*) {WHERE}", ["valor","filas"])
            compare("total", f"SELECT sum(saldo_mes),count(*) {PF} AND fec_informacion=20260331",
                    ["valor","filas"], label="total_pf", persona="PF")
            compare("promedio", f"SELECT avg(saldo_mes) {PF} AND fec_informacion=20260331", ["valor"], persona="PF")
            compare("variacion", f"SELECT fec_informacion/100,sum(saldo_mes) {PF} "
                    "AND fec_informacion IN(20260228,20260331) GROUP BY 1 ORDER BY 1",
                    ["mes","valor"], desde="2026-02", hasta="2026-03", persona="PF", graph="linea")
            compare("neto", f"SELECT sum(saldo_txn) FILTER(WHERE cve_txn IN(400,450))-"
                    f"sum(saldo_txn) FILTER(WHERE cve_txn=300) FROM {MOV} WHERE cve_producto='TDC' AND cve_mes_txn=202605",
                    ["valor"], producto="TDC", conjunto="movimientos", desde="2026-05")
            compare("duplicados", f"SELECT count(*) FROM (SELECT num_cta,cve_producto,fec_informacion {WHERE} "
                    "GROUP BY 1,2,3 HAVING count(*)>1) d", ["grupos_duplicados"], quality=True)
            compare("completitud", f"SELECT count(*),count(*) FILTER(WHERE saldo_mes IS NULL) {WHERE}", ["filas","nulos"], quality=True)
            compare("cobertura", f"SELECT fec_informacion/100,count(*) FROM {BAL} WHERE cve_producto='CHEQUES' "
                    "AND fec_informacion IN(20260228,20260331,20260430) GROUP BY 1 ORDER BY 1",
                    ["mes","filas"], desde="2026-02", hasta="2026-04", quality=True)
            compare("rangos", f"SELECT count(*) FILTER(WHERE saldo_mes NOT BETWEEN 8000 AND 12000) FROM {BAL} "
                    "WHERE cve_producto='CHEQUES' AND fec_informacion=20260131", ["fuera_rango"], desde="2026-01", quality=True)
            compare("atipicos", f"WITH s AS(SELECT saldo_mes v {WHERE}),q AS(SELECT "
                    "percentile_cont(ARRAY[.25,.75]) WITHIN GROUP(ORDER BY v) p FROM s) "
                    "SELECT count(*) FROM s,q WHERE v<p[1]-1.5*(p[2]-p[1]) OR v>p[2]+1.5*(p[2]-p[1])",
                    ["atipicos"], quality=True)
            compare("consistencia", f"SELECT count(*) FROM {BAL} b LEFT JOIN dim.dim_cliente c USING(num_cte) "
                    "LEFT JOIN dim.dim_producto p USING(cve_producto) WHERE b.cve_producto='CHEQUES' AND fec_informacion=20260331 "
                    "AND(c.num_cte IS NULL OR p.cve_producto IS NULL)", ["inconsistencias"], quality=True)
            compare("consistencia", f"WITH net AS (SELECT num_cta,num_cte,sum(CASE WHEN cve_txn=100 THEN saldo_txn "
                    f"WHEN cve_txn=200 THEN -saldo_txn END) AS importe FROM {MOV} WHERE cve_producto='CHEQUES' "
                    f"AND cve_mes_txn=202603 GROUP BY 1,2) SELECT count(*),count(*) FILTER(WHERE importe IS NULL), "
                    f"count(*) FILTER(WHERE b.saldo_mes <> b.saldo_apertura+importe) FROM {BAL} b LEFT JOIN net USING(num_cta,num_cte) "
                    "WHERE cve_producto='CHEQUES' AND fec_informacion=20260331",
                    ["filas","sin_evidencia","inconsistencias"], label="reconciliacion_cierre", quality=True, regla="cierre")
            compare("clientes", "SELECT count(*) FROM dim.dim_cliente GROUP BY tipo_persona ORDER BY tipo_persona",
                    ["valor"], conjunto="clientes", producto="", desde="", agrupar="persona", graph="barras")
            compare("dispersion", f"SELECT avg(saldo_apertura),avg(saldo_mes) FROM {BAL} WHERE fec_informacion=20260331 "
                    "GROUP BY cve_producto ORDER BY cve_producto", ["apertura","cierre"], producto="", agrupar="producto", graph="dispersion")
            hist = execute_plan(dict(DEFAULT, metrica="histograma"))
            chart = chart_for(hist, "histograma")
            values = [row[0] for row in conn.execute(f"SELECT saldo_mes {WHERE} AND saldo_mes IS NOT NULL")]
            low, high = min(values), max(values)
            frequencies = [0] * 10
            for value in values:
                frequencies[min(9, int((value-low)*10/(high-low))) if low != high else 0] += 1
            assert [p["y"] for p in chart["points"]] == frequencies[:len(chart["points"])]
            report["charts"].append(chart)
            report["cases"].append(dict(case="histograma", values=frequencies, sql_seconds=hist["seconds"]))
            identity = load_session("Cliente")
            token = SESSION.set(identity)
            try:
                compare("total", f"SELECT sum(saldo_mes) {WHERE} AND num_cte=%s", ["valor"],
                        label="propio", alcance="propio", params=(identity.customer_id,))
                denied = execute_plan(dict(DEFAULT, metrica="total"))
                assert denied["status"] == "denied" and not denied["sql_executed"]
                report["cases"].append(dict(case="global_cliente_bloqueado"))
            finally:
                SESSION.reset(token)
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["total_seconds"] = time.perf_counter() - started
        (ROOT / "verification-general-sql.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{len(report['cases'])} casos, {report['total_seconds']:.3f}s, passed={report['passed']}")


if __name__ == "__main__":
    main()
