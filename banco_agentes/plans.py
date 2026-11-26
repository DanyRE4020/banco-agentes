"""Planes acotados compilados a SQL parametrizado. El modelo no entrega SQL."""
import calendar
import math
import re
import time
from decimal import Decimal

from .analytics import connect_readonly
from .client_access import (CLIENT_FIELDS, CLIENT_TABLES, PURCHASE_RULE, connect_client,
                            is_employee_purchase_quarter, is_purchase_plan)
from .governance import SESSION, authorize
from .semantics import CUSTOMER_BASIC_FIELDS, CUSTOMER_COMPLETENESS_DETAIL, DATASETS, DIM, JAN, PRODUCTS, SEGMENTS, SIGNED

GROUPS = {
    "producto": "f.cve_producto",
    "mes": "mes",
    "persona": "c.tipo_persona",
    "segmento": "c.segmento_cte",
    "movimiento": "f.desc_txn",
}
PURCHASE_GROUPS = {
    "trimestre": "(f.cve_mes_txn / 100) * 10 + ((mod(f.cve_mes_txn, 100) - 1) / 3 + 1)",
    "anio": "f.cve_mes_txn / 100",
}
METRICS = {
    "total": "sum(valor)", "promedio": "avg(valor)", "minimo": "min(valor)",
    "maximo": "max(valor)", "mediana": "percentile_cont(0.5) WITHIN GROUP (ORDER BY valor)",
    "desviacion": "stddev_samp(valor)", "filas": "count(*)",
    "cuentas": "count(DISTINCT num_cta)", "clientes": "count(DISTINCT num_cte)",
    "neto": "sum(valor)", "variacion": "sum(valor)", "histograma": "", "dispersion": "",
}
CHECKS = ("completitud", "duplicados", "cobertura", "rangos", "atipicos", "consistencia")
CUSTOMER_COMPLETENESS_RULES = {"campos_basicos", CUSTOMER_COMPLETENESS_DETAIL}
COUNT_METRICS = {"filas", "cuentas", "clientes"}
MAX_ROWS = 120


def month(value, minimum):
    if re.fullmatch(r"2026-0[1-6]-[0-9]{2}", value):
        number = int(value[5:7])
        day = int(value[8:])
        if day in {1, calendar.monthrange(2026, number)[1]}:
            value = value[:7]
    if not re.fullmatch(r"2026-0[1-6]", value):
        raise ValueError("Indica un mes disponible con formato AAAA-MM.")
    number = int(value[-2:])
    if number < minimum:
        raise ValueError("Los movimientos disponibles empiezan en febrero de 2026.")
    return number


def normalized_plan(plan, quality=False):
    plan = dict(plan)
    for key in ("desde", "hasta", "producto", "persona", "segmento", "agrupar", "campo"):
        if plan[key] is None:
            plan[key] = ""
        if not isinstance(plan[key], str):
            raise ValueError(f"{key} debe ser texto.")
    if plan["conjunto"] == "saldos" and plan["campo"] == "saldo":
        plan["campo"] = "saldo_mes"
    plan["codigo"] = plan["codigo"] or 0
    plan["base"] = plan.get("base") or "total"
    plan["regla"] = plan.get("regla") or ""
    if plan["regla"] in CUSTOMER_COMPLETENESS_RULES:
        if not quality or plan["metrica"] != "completitud" or plan["conjunto"] != "clientes" or plan["campo"]:
            raise ValueError("La completitud de campos básicos aplica al catálogo de clientes sin elegir una columna.")
    elif plan["regla"] and (plan["metrica"] != "consistencia" or plan["regla"] not in {"referencias", "cierre"}):
        raise ValueError("Las reglas de consistencia disponibles son referencias y cierre.")
    if plan["conjunto"] not in DATASETS:
        raise ValueError("Conjuntos disponibles: saldos, movimientos y clientes.")
    dataset = DATASETS[plan["conjunto"]]
    kind = plan["metrica"]
    if kind not in (CHECKS if quality else METRICS):
        raise ValueError("Capacidad desconocida.")
    if plan["alcance"] not in {"propio", "global"}:
        raise ValueError("Indica alcance propio o global.")
    groups = tuple(part.strip() for part in plan["agrupar"].split(",") if part.strip())
    purchase_group = not quality and (
        is_employee_purchase_quarter(plan, groups)
        or (is_purchase_plan(plan) and groups in {("trimestre",), ("anio",)})
    )
    if "trimestre" in groups and not purchase_group:
        raise ValueError("El trimestre calendario está disponible para el importe total de compras de TDC.")
    if not purchase_group and (len(groups) > 2 or len(set(groups)) != len(groups) or any(g not in GROUPS for g in groups)):
        raise ValueError("Agrupa por hasta dos dimensiones distintas: producto, mes, persona, segmento, movimiento.")
    plan["agrupar"] = groups
    if plan["producto"] and plan["producto"] not in PRODUCTS:
        raise ValueError("Producto desconocido. Usa AHORRO, CHEQUES, TDC o C_PERSONAL.")
    if plan["persona"] not in {"", "PF", "PM"} or plan["segmento"] not in ("", *SEGMENTS):
        raise ValueError("Persona o segmento fuera del catálogo.")
    if plan["codigo"] and (plan["conjunto"] != "movimientos" or plan["codigo"] not in {100, 200, 300, 400, 450}):
        raise ValueError("Código de movimiento fuera del catálogo.")
    if "movimiento" in groups and plan["conjunto"] != "movimientos":
        raise ValueError("La dimensión movimiento solo existe en movimientos.")
    if dataset.month_column:
        minimum = 2 if plan["conjunto"] == "movimientos" else 1
        start = month(plan["desde"], minimum)
        end = month(plan["hasta"] or plan["desde"], minimum)
        if end < start:
            raise ValueError("El fin debe ser igual o posterior al inicio.")
        plan["desde"], plan["hasta"] = f"2026-{start:02d}", f"2026-{end:02d}"
        plan["periodos"] = tuple(range(202600 + start, 202601 + end))
        if kind == "variacion" and (end != start + 1 or groups not in {(), ("mes",)}):
            raise ValueError("La variación requiere dos meses consecutivos y una sola serie.")
        if kind == "variacion":
            if plan["base"] not in METRICS.keys() - {"variacion", "histograma", "dispersion"}:
                raise ValueError("La variación requiere una estadística comparable en ambos cortes.")
            plan["agrupar"] = ("mes",)
        if (not quality and plan["conjunto"] == "saldos" and kind not in COUNT_METRICS
                and start != end and "mes" not in plan["agrupar"]):
            raise ValueError("Los saldos de cierre de distintos meses requieren agrupar por mes.")
    elif plan["desde"] or plan["hasta"] or plan["producto"] or set(groups) & {"mes", "producto"}:
        raise ValueError("La dimensión clientes es estática y no tiene producto. Usa un hecho para clientes por producto.")
    effective_metric = plan["base"] if kind == "variacion" else kind
    if effective_metric == "neto" and plan["conjunto"] != "movimientos":
        raise ValueError("El efecto neto corresponde a movimientos.")
    if plan["regla"] == "cierre" and (plan["conjunto"] != "saldos" or 202601 in plan["periodos"]):
        raise ValueError("Para reconciliar apertura, movimientos y cierre usa saldos de febrero a junio.")
    if not quality and not dataset.measure and kind not in {"filas", "clientes"}:
        raise ValueError("La dimensión clientes admite contar filas o clientes.")
    if kind in {"histograma", "dispersion"}:
        if kind == "histograma" and groups:
            raise ValueError("El histograma admite filtros y un solo conjunto, sin agrupar.")
        if kind == "dispersion" and (plan["conjunto"] != "saldos" or not groups):
            raise ValueError("Dispersión: usa saldos con grupos para comparar promedios de apertura y cierre.")
    if quality and groups:
        raise ValueError("Para Quality delimita el alcance con filtros; cobertura ya desglosa por mes.")
    field = plan["campo"] or dataset.measure or "num_cte"
    if field not in dataset.fields:
        raise ValueError("El campo no pertenece al conjunto.")
    if not quality and plan["campo"] and (field not in {"saldo_mes", "saldo_apertura", "saldo_txn"}
                                           or effective_metric in COUNT_METRICS | {"neto", "dispersion"}):
        raise ValueError("Campo de importe incompatible con la métrica.")
    if quality and kind in {"rangos", "atipicos"} and field not in {"saldo_mes", "saldo_apertura", "saldo_txn"}:
        raise ValueError("Rangos y atípicos requieren un campo de importe.")
    if kind == "rangos":
        initial_bounds = {"CHEQUES": (8000, 12000), "AHORRO": (5, 10000),
                          "TDC": (10000, 120000), "C_PERSONAL": (50000, 85000)}
        if (plan["minimo"] is None and plan["maximo"] is None and
                plan["conjunto"] == "saldos" and plan["periodos"] == (202601,) and plan["producto"]):
            plan["minimo"], plan["maximo"] = initial_bounds[plan["producto"]]
            plan["fuente_regla"] = f"{JAN}:16-21"
        if (plan["minimo"] is None or plan["maximo"] is None or
                not math.isfinite(plan["minimo"]) or not math.isfinite(plan["maximo"]) or
                plan["minimo"] > plan["maximo"]):
            raise ValueError("Indica dos límites finitos ordenados o un producto de saldos de enero con rango documentado.")
    plan["campo"] = "" if plan["regla"] in CUSTOMER_COMPLETENESS_RULES else field
    return plan


def base_query(plan):
    dataset = DATASETS[plan["conjunto"]]
    is_client_dimension = plan["conjunto"] == "clientes"
    person_join = not is_client_dimension and (
        plan["persona"] or plan["segmento"] or {"persona", "segmento"} & set(plan["agrupar"])
    )
    prefix = "c" if is_client_dimension else "f"
    source = CLIENT_TABLES[plan["conjunto"]] if plan["alcance"] == "propio" else dataset.table
    tables = f"{source} {prefix}"
    if person_join:
        tables += " LEFT JOIN dim.dim_cliente c ON c.num_cte = f.num_cte"
    where, params = [], []
    for column, value in (
        ("f.cve_producto", plan["producto"]), ("c.tipo_persona", plan["persona"]),
        ("c.segmento_cte", plan["segmento"]), ("f.cve_txn", plan["codigo"]),
    ):
        if value:
            where.append(f"{column} = %s")
            params.append(value)
    if dataset.month_column:
        if plan["conjunto"] == "saldos":
            cuts = [m * 100 + calendar.monthrange(m // 100, m % 100)[1] for m in plan["periodos"]]
            where.append("f.fec_informacion = ANY(%s)")
            params.append(cuts)
        else:
            where.append("f.cve_mes_txn = ANY(%s)")
            params.append(list(plan["periodos"]))
    if plan["alcance"] == "propio":
        where.append("f.num_cte = %s")
        params.append(SESSION.get().num_cte)
    month_expr = f"f.{dataset.month_column}" if dataset.month_column else "NULL::int"
    value = "NULL::numeric"
    if dataset.measure:
        measure = plan["base"] if plan["metrica"] == "variacion" else plan["metrica"]
        value = SIGNED if measure == "neto" else f"f.{plan['campo']}"
    groups = []
    for group in plan["agrupar"]:
        if group != "mes":
            expression = PURCHASE_GROUPS[group] if group in PURCHASE_GROUPS else GROUPS[group]
            groups.append(f"{expression} AS {group}")
    fields = CLIENT_FIELDS[plan["conjunto"]] if plan["alcance"] == "propio" else dataset.fields
    columns = [f"{prefix}.{col}" for col in fields]
    columns += [f"{value} AS valor", f"{month_expr} AS mes", *groups]
    suffix = " WHERE " + " AND ".join(where) if where else ""
    return "WITH base AS (SELECT " + ", ".join(columns) + f" FROM {tables}{suffix})", params, person_join


def analytics_query(plan):
    metric = plan["metrica"]
    groups = ", ".join(plan["agrupar"])
    group_by = f" GROUP BY {groups} ORDER BY {groups}" if groups else ""
    select = groups + ", " if groups else ""
    if metric == "histograma":
        return """
        , bounds AS (SELECT min(valor) AS low, max(valor) AS high FROM base)
        , bins AS (
          SELECT i, low + (high-low)*i/10 AS desde, low + (high-low)*(i+1)/10 AS hasta,
                 low, high
          FROM bounds CROSS JOIN generate_series(0,9) AS i
          WHERE low IS NOT NULL AND (low <> high OR i=0)
        ), counts AS (
          SELECT CASE WHEN low=high THEN 0
                 ELSE least(9, floor((valor-low)*10/nullif(high-low,0))::int) END AS i,
                 count(*) AS frecuencia
          FROM base CROSS JOIN bounds WHERE valor IS NOT NULL GROUP BY 1
        )
        SELECT desde, hasta, coalesce(frecuencia,0) AS frecuencia
        FROM bins LEFT JOIN counts USING (i) ORDER BY i
        """
    if metric == "dispersion":
        return (f" SELECT {select}avg(saldo_apertura) AS apertura, avg(saldo_mes) AS cierre, count(*) AS filas"
                " FROM base WHERE saldo_apertura IS NOT NULL AND saldo_mes IS NOT NULL" + group_by)
    aggregate = METRICS[plan["base"] if metric == "variacion" else metric]
    return f" SELECT {select}{aggregate} AS valor, count(*) AS filas, count(valor) AS observados FROM base{group_by}"


def quality_query(plan, params):
    rule = plan["metrica"]
    field = plan["campo"]
    key = ", ".join(DATASETS[plan["conjunto"]].key)
    if rule == "duplicados":
        return (f", grupos AS (SELECT count(*) AS n FROM base GROUP BY {key} HAVING count(*)>1)"
                " SELECT (SELECT count(*) FROM base) AS filas, count(*) AS grupos_duplicados,"
                " coalesce(sum(n-1),0) AS filas_adicionales FROM grupos",
                f"Clave esperada del generador: ({key}). Las filas adicionales cuentan a partir de la segunda.")
    if rule == "completitud":
        if plan["regla"] in CUSTOMER_COMPLETENESS_RULES:
            missing = " OR ".join(f"{column} IS NULL" for column in CUSTOMER_BASIC_FIELDS)
            totals = f"count(*) AS filas, count(*) FILTER (WHERE {missing}) AS incompletos"
            query = f" SELECT {totals} FROM base"
            if plan["regla"] == CUSTOMER_COMPLETENESS_DETAIL:
                counts = ", ".join(f"count(*) - count({column}) AS {column}" for column in CUSTOMER_BASIC_FIELDS)
                values = ", ".join(f"('{column}', conteos.{column})" for column in CUSTOMER_BASIC_FIELDS)
                query = (
                    f", conteos AS (SELECT {totals}, {counts} FROM base)"
                    " SELECT filas, incompletos, (SELECT jsonb_agg(jsonb_build_object('campo', campo, 'nulos', nulos)"
                    " ORDER BY nulos DESC, campo)"
                    f" FROM (VALUES {values}) AS campos(campo, nulos)) AS faltantes_por_campo FROM conteos"
                )
            return (query,
                    "Criterio de esta revisión: un registro es incompleto si falta algún campo que el generador "
                    "puebla para cada cliente. Campos revisados: " + ", ".join(CUSTOMER_BASIC_FIELDS) + ".")
        return (f" SELECT count(*) AS filas, count(*)-count({field}) AS nulos FROM base",
                f"Regla propuesta: {field} debe estar presente. Solo NULL; no mide cadenas vacías.")
    if rule == "cobertura":
        if not DATASETS[plan["conjunto"]].month_column:
            raise ValueError("Cobertura temporal requiere un conjunto mensual.")
        params.append(list(plan["periodos"]))
        return (", esperados AS (SELECT unnest(%s::int[]) AS mes)"
                " SELECT e.mes, count(b.mes) AS filas FROM esperados e LEFT JOIN base b USING (mes)"
                " GROUP BY e.mes ORDER BY e.mes",
                "Regla propuesta: cada mes del intervalo debe tener al menos una fila en el alcance. "
                "No comprueba cobertura por cuenta.")
    if rule == "rangos":
        params.extend((plan["minimo"], plan["maximo"]))
        return (f" SELECT count(*) AS filas, count({field}) AS observados, min({field}) AS minimo,"
                f" max({field}) AS maximo, count(*) FILTER (WHERE {field}<%s OR {field}>%s) AS fuera_rango FROM base",
                (f"Regla documentada en {plan['fuente_regla']}. " if "fuente_regla" in plan else "Regla propuesta. ")
                + f"{field} entre {plan['minimo']} y {plan['maximo']} inclusive. Excluye NULL del rango.")
    if rule == "atipicos":
        return (f", limites AS (SELECT percentile_cont(.25) WITHIN GROUP (ORDER BY {field}) AS q1,"
                f" percentile_cont(.75) WITHIN GROUP (ORDER BY {field}) AS q3 FROM base)"
                f" SELECT q1, q3, count(*) AS filas, count({field}) AS observados,"
                f" count(*) FILTER (WHERE {field} < q1-1.5*(q3-q1) OR {field} > q3+1.5*(q3-q1)) AS atipicos"
                " FROM base CROSS JOIN limites GROUP BY q1,q3",
                "Regla propuesta: cercas de Tukey de 1,5 IQR dentro del alcance, excluyendo NULL. "
                "Un atípico estadístico no implica un error.")
    if plan["regla"] == "cierre":
        params.append(list(plan["periodos"]))
        return (f""", movimientos AS (
            SELECT f.num_cta, f.num_cte, f.cve_producto, f.cve_mes_txn AS mes, sum({SIGNED}) AS neto,
                   count(*) AS n, count({SIGNED}) AS observados, count(DISTINCT f.cve_txn) AS tipos
            FROM cfg.fact_txn_captacoloca_d f
            WHERE f.cve_mes_txn = ANY(%s)
            GROUP BY f.num_cta, f.num_cte, f.cve_producto, f.cve_mes_txn
        ), revision AS (
            SELECT b.saldo_mes, b.saldo_apertura, m.neto,
                   m.n = m.observados AND m.n = m.tipos
                       AND m.tipos = CASE WHEN b.cve_producto='TDC' THEN 3 ELSE 2 END
                       AND b.saldo_mes IS NOT NULL AND b.saldo_apertura IS NOT NULL AS verificable
            FROM base b LEFT JOIN movimientos m USING (num_cta, num_cte, cve_producto, mes)
        )
        SELECT count(*) AS filas, count(*) FILTER (WHERE verificable IS NOT TRUE) AS sin_evidencia,
               count(*) FILTER (WHERE verificable AND saldo_mes <> saldo_apertura + neto) AS inconsistencias
        FROM revision""",
        "Regla del generador: cierre = apertura + efecto neto de movimientos por cuenta, producto y mes. "
        "Se requiere un movimiento válido por tipo esperado.")
    if plan["conjunto"] == "clientes":
        return (" SELECT count(*) AS filas, count(*) FILTER (WHERE tipo_persona IS NULL"
                " OR tipo_persona NOT IN ('PF','PM')) AS inconsistencias FROM base",
                "Tipo de persona dentro de PF y PM. El CHECK permite NULL; se señala como incompleto.")
    return (" SELECT count(*) AS filas, count(*) FILTER (WHERE NOT EXISTS"
            " (SELECT 1 FROM dim.dim_cliente c WHERE c.num_cte = base.num_cte)"
            " OR NOT EXISTS (SELECT 1 FROM dim.dim_producto p WHERE p.cve_producto = base.cve_producto)) AS inconsistencias FROM base",
            "Regla propuesta de consistencia referencial: cada hecho debe tener cliente y producto en las dimensiones.")


def execute_plan(raw, *, quality=False):
    started = time.perf_counter()
    scope = raw.get("alcance")
    operation = "quality" if quality else "own_analysis" if scope == "propio" else "aggregate_balance"
    denial = authorize(operation)
    if denial:
        return {**denial, "sql_executed": False}
    try:
        plan = normalized_plan(raw, quality)
        if SESSION.get().profile == "Cliente" and (
            plan["conjunto"] == "clientes" or plan["persona"] or plan["segmento"]
            or set(plan["agrupar"]) & {"persona", "segmento"}
            or plan["metrica"] in {"clientes", "histograma", "dispersion"}
        ):
            return {"status": "denied", "message": "El perfil Cliente solo dispone de métricas de sus propios saldos y movimientos.",
                    "sql_executed": False}
        if scope == "propio" and plan["conjunto"] == "saldos" and not plan["producto"] and "producto" not in plan["agrupar"]:
            return {"status": "invalid_plan", "message": "Agrupa los saldos por producto para separar saldos a favor y deudas.",
                    "sql_executed": False}
        prefix, params, joined = base_query(plan)
        if quality:
            query, rule = quality_query(plan, params)
        else:
            query, rule = analytics_query(plan), None
    except (ValueError, TypeError) as error:
        return {"status": "invalid_plan", "message": str(error), "sql_executed": False}
    sql_executed = False
    try:
        connector = connect_client if scope == "propio" else connect_readonly
        with connector() as connection:
            if connection.execute("SHOW transaction_read_only").fetchone()[0] != "on":
                raise ValueError("transaction_read_only")
            sql_executed = True
            cursor = connection.execute(prefix + query + f" LIMIT {MAX_ROWS + 1}", params)
            columns = [col.name for col in cursor.description]
            rows = [dict(zip(columns, (format(value, "f") if isinstance(value, Decimal) else value for value in row)))
                    for row in cursor.fetchall()]
    except Exception:
        return {"status": "unavailable", "message": "No se pudo verificar el plan en PostgreSQL de solo lectura.",
                "sql_executed": sql_executed, "seconds": time.perf_counter() - started}
    dataset = DATASETS[plan["conjunto"]]
    sources = list(dataset.sources)
    if plan["regla"] == "cierre":
        sources.extend((f"{DATASETS['movimientos'].table} — movimientos de la reconciliación",
                        DATASETS["movimientos"].sources[1]))
    if joined or (quality and plan["metrica"] == "consistencia"):
        sources.append(f"{DIM}:12-35,82-98")
    status = "ok"
    notes = []
    purchases = is_purchase_plan(plan)
    if purchases or is_employee_purchase_quarter(plan, plan["agrupar"]):
        notes.append(PURCHASE_RULE)
        rule = PURCHASE_RULE
    elif scope == "propio" and plan["conjunto"] == "saldos":
        notes.append("AHORRO y CHEQUES son productos de captación. TDC y C_PERSONAL son créditos.")
    effective_metric = plan["base"] if plan["metrica"] == "variacion" else plan["metrica"]
    units = {"clientes": "Clientes", "cuentas": "Cuentas", "filas": "Registros"}
    if not quality and "mes" in plan["agrupar"]:
        missing = set(plan["periodos"]) - {r["mes"] for r in rows}
        if missing and (rows or not purchases):
            status = "partial"
            notes.append("Cortes sin filas: " + ", ".join(map(str, sorted(missing))) + ".")
    if len(rows) > MAX_ROWS:
        status = "partial"
        notes.append(f"Primeras {MAX_ROWS} filas. Reduce filtros o dimensiones para ver el conjunto completo.")
    if not rows or all(row.get("filas") == 0 for row in rows):
        status = "insufficient"
        if not purchases:
            notes.append("El alcance no contiene observaciones. Un resultado vacío no prueba ausencia de anomalías.")
    if not quality and effective_metric not in COUNT_METRICS and any(
        "valor" in row and (row["valor"] is None or row["observados"] < row["filas"]) for row in rows
    ):
        status = "partial" if status == "ok" else status
        notes.append("Hay importes faltantes o códigos sin signo documentado; la métrica excluye esas observaciones.")
    if not quality and plan["metrica"] == "variacion":
        if len(rows) != 2 or any(row["valor"] is None for row in rows):
            status = "insufficient"
            notes.append("Se requieren datos de los dos cortes para comparar.")
        elif status == "ok":
            previous, current = (Decimal(str(row["valor"])) for row in rows)
            notes.append(f"Cambio absoluto: {current - previous}.")
            from .variation import percentage
            relative = percentage(previous, current)
            notes.append(f"Cambio relativo: {relative:.4f} %."
                         if relative is not None else "El cambio relativo no está definido con base cero.")
    if plan["regla"] == "cierre" and rows and rows[0]["sin_evidencia"]:
        status = "partial" if status == "ok" else status
        notes.append("Los casos sin evidencia se excluyeron del conteo de inconsistencias.")
    if quality and plan["metrica"] == "atipicos" and (
        not rows or rows[0]["observados"] < 4 or rows[0]["q1"] == rows[0]["q3"]
    ):
        status = "insufficient"
        notes.append("Las cercas no se interpretan con menos de cuatro observaciones o IQR cero.")
    return {"status": status, "plan": plan, "rows": rows[:MAX_ROWS], "columns": columns,
            "table": CLIENT_TABLES[plan["conjunto"]] if scope == "propio" else dataset.table,
            "sources": sources, "rule": rule, "notes": notes,
            "units": "Registros" if quality else units.get(effective_metric, "Importe sintético, moneda sin definir"),
            "sql_executed": True, "seconds": time.perf_counter() - started}
