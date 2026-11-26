"""Ranking acotado de Analytics con explicación de política a cargo de Steward."""
import asyncio
import re

from .analytics import rank_employee_customers
from .catalog import normalize
from .governance import SESSION, employee_individual_request, employee_ranking_requested
from .presentation import decimal_text, source_entry
from .semantics import DATASETS, PRODUCTS
from .steward_rag import employee_access_policy

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio")


def ranking_scope(question):
    text = normalize(question)
    products = [product for product in PRODUCTS if re.search(r"\b" + product.lower() + r"\b", text)]
    months = {f"2026-{i:02d}" for i, month in enumerate(MONTHS, 1) if re.search(r"\b" + month + r"\b", text)}
    months.update(re.findall(r"\b2026-0[1-6]\b", text))
    years = set(re.findall(r"\b20\d{2}\b", text))
    count = re.search(r"\btop[\s-]+(\d+)\b", text)
    if (len(products) != 1 or len(months) != 1 or years - {"2026"}
            or (count and count[1] != "5") or not re.search(r"\bsaldos?\b", text)):
        return None
    return products[0], months.pop()


async def employee_turn(question, trace, *, after_ranking=False):
    profile = SESSION.get().profile
    individual = profile == "Analista" and employee_individual_request(question, after_ranking=after_ranking)
    if not individual and not employee_ranking_requested(question):
        return None

    trace.records.append({"event": "agent_started", "agent": "data_steward"})
    rule = "DG-01" if profile == "Analista" else "DG-02"
    policy, citation = employee_access_policy(rule)
    trace.records.append({"event": "tool_completed", "agent": "data_steward",
                          "tool": "consultar_politica_acceso", "rule": rule})
    response = {"answer": "", "charts": [], "tables": [], "sources": [citation],
                "presentation": {"format": "prosa", "objective": "resumen", "result_ids": []}}
    reference = f"{citation['label']}, {citation['location']}"
    if individual or profile != "Analista":
        trace.records.append({"event": "access_blocked", "agent": "policy_code",
                              "reason": "employee_individual" if individual else "employee_only",
                              "profile": profile, "sql_executed": False})
        response["answer"] = f"Acceso bloqueado. {policy}\n\nFuente: {reference}."
        return response

    scope = ranking_scope(question)
    if not scope:
        response["answer"] = "Para mostrar los cinco primeros puestos, indica un producto y un mes de enero a junio de 2026."
        return response

    product, month = scope
    trace.records.append({"event": "agent_started", "agent": "data_analytics"})
    result = await asyncio.to_thread(rank_employee_customers, product, month)
    trace.records.extend((
        {"event": "validated_plan", "agent": "data_analytics", "status": result["status"],
         "plan": {"metrica": "ranking_saldos", "producto": product, "desde": month, "hasta": month,
                  "alcance": "global"}, "sql_executed": result["sql_executed"]},
        {"event": "tool_completed", "agent": "data_analytics", "tool": "ranking_saldos"},
    ))
    rows = result.get("rows", [])
    if not rows:
        response["answer"] = ("No hay saldos en ese corte." if result["status"] == "ok"
                              else "No se pudo verificar el ranking en PostgreSQL de solo lectura.")
        return response

    title = f"Ranking de saldos de {product} al cierre de {month}"
    response["answer"] = f"{title}. {policy}\n\nFuente: {reference}.\n\nDatos sintéticos."
    response["tables"] = [{"title": title, "truncated": False,
                           "columns": [{"key": "posicion", "label": "Puesto"},
                                       {"key": "alias", "label": "Titular"},
                                       {"key": "saldo", "label": "Saldo"}],
                           "rows": [{**row, "saldo": decimal_text(row["saldo"])} for row in rows]}]
    response["sources"].append(source_entry(DATASETS["saldos"].table))
    response["presentation"]["format"] = "tabla"
    return response
