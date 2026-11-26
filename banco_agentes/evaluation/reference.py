"""Oráculo independiente: SELECT revisados, sin SQL del modelo ni del CSV."""
import calendar
import hashlib
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

CSV_SHA256 = "8ee394cf3fc59940045108dadd0be025301ab9f70ee99dbe703b61f2d39fdf53"
SQL = Path(__file__).with_name("sql")


@dataclass(frozen=True)
class Spec:
    products: tuple
    months: tuple
    style: str
    person: str = ""


SPECS = {
    "BA-001": Spec(("CHEQUES", "AHORRO"), (6,), "pair"),
    "BA-002": Spec(("CHEQUES", "AHORRO"), (5,), "pair"),
    "BA-003": Spec(("TDC",), tuple(range(1, 7)), "line"),
    "BA-004": Spec(("AHORRO",), tuple(range(1, 7)), "line"),
    "BA-011": Spec(("TDC",), (2, 3), "false_premise"),
    "BA-012": Spec(("AHORRO", "TDC"), tuple(range(2, 7)), "growth"),
    "BA-013": Spec(("CHEQUES",), (6,), "statistics"),
    "BA-016": Spec(("CHEQUES",), (3,), "total"),
    "BA-017": Spec(("CHEQUES",), (3,), "total", "PF"),
    "BA-018": Spec(("AHORRO",), (4, 5), "quality"),
    "BA-020": Spec(("TDC",), tuple(range(1, 7)), "duplicates"),
    "BA-021": Spec(("CHEQUES", "AHORRO"), (6,), "pair"),
}


def sql_reference(case, connector=None):
    spec = SPECS.get(case.id)
    if spec is None:
        return {"status": "no_aplica", "reason": "No corresponde un cálculo SQL en este caso; BA-015 requiere aclarar el corte."}
    if case.row["perfil"] != "Analista":
        return {"status": "no_verificable", "reason": "No hay un oráculo de Cliente revisado; no se ejecutan agregados globales."}
    if connector is None:
        from banco_agentes.analytics import connect_readonly
        connector = connect_readonly
    query_name = "estadisticas.sql" if spec.style == "statistics" else "calidad.sql" if spec.style in {"quality", "duplicates"} else "totales.sql"
    query = (SQL / query_name).read_text(encoding="utf-8")
    cuts = [20260000 + m * 100 + calendar.monthrange(2026, m)[1] for m in spec.months]
    params = ((spec.products[0], cuts[0]) if spec.style == "statistics" else
              (spec.products[0], cuts) if spec.style in {"quality", "duplicates"} else
              (list(spec.products), cuts, spec.person, spec.person))
    report = {"status": "no_verificable", "query_file": f"evaluation/sql/{query_name}",
              "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "parameters": params,
              "table": "cfg.fact_sdos_captacoloca_m", "rule_version": "evaluacion-csv-v1"}
    try:
        with connector() as db:
            user, readonly = db.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
            if user != "banco_lectura" or readonly != "on":
                raise ValueError("El oráculo requiere banco_lectura y transacción de solo lectura.")
            rows = db.execute(query, params).fetchall()
        facts = []
        if spec.style == "statistics":
            for segment, median, deviation in rows:
                for metric, value in (("mediana", median), ("desviacion", deviation)):
                    facts.append(dict(product="CHEQUES", month=202606, metric=metric, segment=segment, value=str(value)))
        elif spec.style in {"quality", "duplicates"}:
            for month, nulls, duplicates, count in rows:
                metrics = (("completitud", nulls), ("duplicados", duplicates)) if spec.style == "quality" else (("duplicados", duplicates),)
                facts.extend(dict(product=spec.products[0], month=month, metric=metric, value=str(value), count=count) for metric, value in metrics)
        else:
            for product, month, value, count, observed in rows:
                if value is None or count != observed or not count:
                    raise ValueError("El SQL de referencia no tiene observaciones completas.")
                facts.append(dict(product=product, month=month, metric="total", value=str(value), count=count))
        if not facts or any(not Decimal(f["value"]).is_finite() for f in facts):
            raise ValueError("Referencia insuficiente.")
        expected_size = len(spec.products) * len(spec.months) * (2 if spec.style == "quality" else 1)
        if spec.style != "statistics" and len(facts) != expected_size:
            raise ValueError("Faltan productos o cortes en SQL.")
        derived = {}
        if spec.style == "pair":
            values = {f["product"]: Decimal(f["value"]) for f in facts}
            derived = {"winner": max(values, key=values.get), "difference": str(max(values.values()) - min(values.values()))}
        if spec.style in {"growth", "false_premise", "line"}:
            for product in spec.products:
                start = next(Decimal(f["value"]) for f in facts if f["product"] == product and f["month"] == 202600 + min(spec.months))
                end = next(Decimal(f["value"]) for f in facts if f["product"] == product and f["month"] == 202600 + max(spec.months))
                with localcontext() as context:
                    context.prec = 40
                    derived[product] = {"absolute": str(end - start), "percent": str((end - start) / start * 100) if start else None}
        if spec.style == "growth":
            derived["growth_winners"] = {
                metric: max(spec.products, key=lambda p: Decimal(derived[p][metric]))
                if all(derived[p][metric] is not None for p in spec.products) else None
                for metric in ("absolute", "percent")}
        if spec.style == "line":
            derived["maximum"] = max(facts, key=lambda f: Decimal(f["value"]))
        report.update(status="ok", reader_role_verified=True, transaction_read_only=readonly, facts=facts, derived=derived)
    except Exception as error:
        report.update(error_type=type(error).__name__, reason="No se obtuvo evidencia SQL independiente completa.")
    return report
