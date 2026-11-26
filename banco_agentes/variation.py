"""Variación porcentual derivada de saldos verificados."""
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, localcontext


def percentage(base, final):
    if base is None or final is None:
        return None
    base, final = Decimal(str(base)), Decimal(str(final))
    if not base or not base.is_finite() or not final.is_finite():
        return None
    with localcontext() as context:
        context.prec = max(40, len(str(base)) + len(str(final)) + 8)
        return (final - base) / base * 100


@dataclass(frozen=True)
class VariationRequest:
    mode: str
    plot: bool = False
    kind: str = "auto"
    formula: bool = False
    followup: str = ""
    compare: bool = False
    calculation: bool = False
    products: tuple = ()
    months: tuple = ()
    years: tuple = ()
    show_max: bool = False

    @property
    def graph_kind(self):
        if self.mode == "mensual":
            return "barras" if self.kind == "barras" else "linea"
        return "barras"


def variation_request(question, previous_mode=None, *, has_balances=False):
    from .presentation import MONTHS
    from .semantics import PRODUCTS

    text = "".join(c for c in unicodedata.normalize("NFKD", question.casefold())
                   if not unicodedata.combining(c)).strip(" ¿?¡!.")
    text = re.sub(r"^(?:y\s+)?ahora(?:\s*,\s*|\s+)", "", text)
    plot = bool(re.search(r"\bgrafica[rl]?\w*\b|\b(?:en|con) (?:una )?(?:barras|lineas)\b", text))
    monthly = bool(re.search(r"mes a mes|mes contra mes|(?:variacion|crecimiento|cambio|tasa) mensual|intermensual", text))
    calculation_command = r"(?:muestrame|muestra|ensename|detalla|desglosa|explicame|explica) el calculo(?: paso a paso)?"
    calculation = bool(re.search(rf"\b{calculation_command}\b", text))
    formula = calculation or bool(re.search(r"\bformula\b|como (?:se calcula|lo calculaste|se obtiene)", text))
    products = "|".join(code.lower() for code in PRODUCTS)
    balance = bool(re.search(r"\bsaldos?\b|\b(?:" + products + r")\b", text))
    growth = bool(re.search(r"\b(?:crec\w*|variacion|cambi\w*|aument\w*|disminu\w*)\b", text))
    explicit_rate = bool(re.search(r"\b(?:porcent\w*|tasa)\b|%", text))
    reference = bool(re.fullmatch(r"(?:grafica|graficalos|muestra|pon) (?:esos|estos|los) porcentajes(?: en (?:barras(?: agrupadas)?|lineas))?", text))
    continuation = bool(re.fullmatch(
        r"(?:grafica|muestra|calcula) (?:el |la )?(?:crecimiento|variacion|cambio)(?: porcentual)? (?:mes a mes|mensual)(?: (?:en|con) (?:barras(?: agrupadas)?|lineas))?", text))
    kind = "barras" if re.search(r"\bbarras\b", text) else "linea" if re.search(r"\blineas?\b", text) else "auto"
    if reference:
        return VariationRequest(previous_mode or "periodo", plot, kind, followup="tasas")
    if previous_mode and re.fullmatch(rf"(?:y )?{calculation_command}", text):
        return VariationRequest(previous_mode, formula=True, followup="tasas", calculation=True)
    if previous_mode and formula and not balance and re.fullmatch(
            r"(?:y )?(?:cual es |explica(?:me)? )?la formula|como (?:se calcula|lo calculaste|se obtiene)", text):
        return VariationRequest(previous_mode, plot, kind, formula, followup="tasas")
    if continuation and has_balances:
        return VariationRequest("mensual", plot, kind, formula, followup="saldos")
    # Reservar el crecimiento absoluto para el flujo existente.
    full_period = bool(re.search(r"\bcrecimiento (?:del|en el) periodo\b", text))
    if balance and growth and "absolut" not in text and (explicit_rate or monthly or full_period
                                                        or "crecimiento" in text and (plot or formula)):
        compare = bool(re.search(r"compar|\b(?:brecha|diferencia)\b", text))
        previous_balances = re.search(
            r"\b(?:estos|esos|aquellos) saldos\b|\bsaldos (?:ya )?(?:mostrad[oa]s|consultad[oa]s|"
            r"guardad[oa]s|anteriores|previos|que (?:ya )?(?:mostraste|consultaste))\b", text)
        filters = re.search(r"\b(?:segmentos?|personas?|pf|pm|codigo|filtros?)\b", text)
        if previous_balances and not filters:
            selected_products = tuple(dict.fromkeys(p.upper() for p in re.findall(rf"\b(?:{products})\b", text)))
            named = {i for i, name in enumerate(MONTHS) if name and re.search(rf"\b{name}\b", text)}
            numeric = {int(m) for m in re.findall(r"\b20\d{2}-(0[1-9]|1[0-2])\b", text)}
            years = tuple(dict.fromkeys(re.findall(r"\b20\d{2}\b", text)))
            return VariationRequest("mensual" if monthly else "periodo", plot, kind, formula,
                                    followup="saldos_mostrados", compare=compare, calculation=calculation,
                                    products=selected_products, months=tuple(sorted(named | numeric)), years=years,
                                    show_max=bool(re.search(r"\b(?:crecio|aumento|subio) mas\b|\bmayor crecimiento\b", text)))
        return VariationRequest("mensual" if monthly else "periodo", plot, kind, formula,
                                compare=compare, calculation=calculation)
    return None


def variation_report(results, mode, *, include_calculation=False, products=()):
    from .charts import balance_rows, balance_chart

    if not results or any(result.get("agent") != "data_analytics" for result in results):
        raise ValueError("La variación requiere saldos verificados por Analytics.")
    rows = [balance_rows(result) for result in results]
    if any(block is None for block in rows):
        raise ValueError("La variación requiere saldos mensuales compatibles y verificados por producto.")
    # Un agregado calculado con importes omitidos no sirve como saldo completo de un corte.
    complete = [[dict(row, valor=None) if raw.get("observados", 0) < raw.get("filas", 0) else row
                 for row, raw in zip(block, result["rows"], strict=True)] for block, result in zip(rows, results)]
    if products:
        complete = [[row for row in block if row["producto"] in products] for block in complete]
    base = balance_chart(results, complete, "barras", require_value=False)
    cuts = base["series"][0]["x"]
    if len(cuts) < 2:
        raise ValueError("Indica un saldo base y un saldo final de meses distintos para calcular el porcentaje.")
    series = []
    for block in base["series"]:
        values = block["y"]
        pairs = ([(0, len(cuts) - 1)] if mode == "periodo"
                 else [(i - 1, i) for i in range(len(cuts))])
        rates, reasons = [], []
        for start, end in pairs:
            if start < 0:
                rate, reason = None, "sin mes anterior en el periodo"
            elif values[start] is None or values[end] is None:
                rate, reason = None, "falta un saldo necesario"
            else:
                rate = percentage(values[start], values[end])
                reason = None if rate is not None else "saldo base cero"
            if rate is None:
                rates.append(None)
            else:
                with localcontext() as context:
                    context.prec = max(28, rate.adjusted() + 4)
                    rounded = rate.quantize(Decimal("0.01"))
                    rates.append(format(rounded or Decimal("0.00"), "f"))
            reasons.append(reason)
        calculated = {**block, "x": [cuts[end] for _, end in pairs], "y": rates, "reasons": reasons,
                      "result_ids": [block["result_ids"][end] for _, end in pairs]}
        if include_calculation:
            calculated["calculations"] = [
                {"base_month": cuts[start], "base": values[start], "final_month": cuts[end], "final": values[end],
                 "rate": rate, "reason": reason}
                for (start, end), rate, reason in zip(pairs, rates, reasons) if start >= 0
            ]
        series.append(calculated)
    return {**base, "series": series, "rate_mode": mode, "cuts": cuts, "metric": "variacion_porcentual",
            "units": "%", "x_label": "Mes" if mode == "mensual" else "Cierre del periodo",
            "y_label": "Variación (%)", "title": "Variación mensual" if mode == "mensual" else "Crecimiento del periodo"}


def rate_chart(report, kind):
    points = [{"label": f"{s['name']} / {month}", "x": None, "y": value, "result_id": key}
              for s in report["series"] for month, value, key in zip(s["x"], s["y"], s["result_ids"])]
    return {**report, "type": kind, "points": points}


def variation_text(report, formula, *, compare=False, show_max=False, chat=False, with_table=False):
    from .presentation import chat_decimal_text, decimal_text, month_text

    number = chat_decimal_text if chat else decimal_text
    percent = "%" if chat else " %"
    paragraph = "\n\n" if chat else " "
    multiple = len(report["series"]) > 1
    cuts = report["cuts"]
    label = "Variación mensual" if report["rate_mode"] == "mensual" else "Crecimiento del periodo"
    sentence = f"{label} entre {month_text(cuts[0])} y {month_text(cuts[-1])}"
    text = sentence + "."
    if not with_table:
        items = []
        for series in report["series"]:
            values = [f"{number(value)}{percent}" if value is not None else f"no calculable ({reason})"
                      for value, reason in zip(series["y"], series["reasons"])]
            if report["rate_mode"] == "mensual":
                values = [f"{month_text(month).split(' de ', 1)[0]} {value}" for month, value in zip(series["x"], values)]
            items.append(f"{series['name']}: {', '.join(values)}.")
        text += paragraph + ("\n".join("- " + item for item in items) if chat and multiple else " ".join(items))
    if report["rate_mode"] == "periodo":
        rates = [Decimal(s["y"][0]) for s in report["series"] if s["y"][0] is not None]
        if (show_max or chat) and len(rates) == len(report["series"]) and multiple:
            maximum = max(rates)
            leaders = [s["name"] for s in report["series"] if Decimal(s["y"][0]) == maximum]
            if len(leaders) == 1:
                finding = f"{leaders[0]} fue el producto que más creció, con {number(maximum)}{percent}."
                if chat and maximum <= 0:
                    finding = f"{leaders[0]} registró la mayor variación, con {number(maximum)}%."
            else:
                change = "variación" if chat and maximum <= 0 else "crecimiento"
                gender = "la" if change == "variación" else "el"
                finding = f"{' y '.join(leaders)} comparten {gender} mayor {change}, con {number(maximum)}{percent}."
            text = finding + paragraph + text if chat else text + " " + finding
        elif chat and multiple:
            text = "Faltan datos para comparar todos los productos." + paragraph + text
        if len(rates) >= 2:
            text += paragraph + f"La brecha{' entre la tasa mayor y la menor' if len(rates) > 2 else ''} es de {number(max(rates) - min(rates))} puntos porcentuales."
        elif len(report["series"]) > 1:
            text += paragraph + "La brecha no es calculable."
    elif compare and len(report["series"]) > 1:
        spreads = []
        for index, month in enumerate(cuts):
            values = [s["y"][index] for s in report["series"]]
            if all(value is not None for value in values):
                rates = [Decimal(value) for value in values]
                spreads.append(f"{month_text(month).split(' de ', 1)[0]} {number(max(rates) - min(rates))}")
        text += paragraph + ("Brecha mensual en puntos porcentuales: " + ", ".join(spreads) + "." if spreads
                 else "La brecha mensual no es calculable.")
    if formula:
        base = "del mes anterior" if report["rate_mode"] == "mensual" else "del inicio del periodo"
        text += paragraph + f"Fórmula: (saldo final − saldo base) / saldo base × 100. El saldo base es el {base}."
    for series in report["series"]:
        for calculation in series.get("calculations", []):
            start = month_text(calculation["base_month"])
            end = month_text(calculation["final_month"])
            base = number(calculation["base"])
            final = number(calculation["final"])
            text += f"\n\n{series['name']}. Saldo inicial ({start}): {base}. Saldo final ({end}): {final}."
            if calculation["rate"] is None:
                text += f" Porcentaje no calculable ({calculation['reason']})."
            else:
                rate = number(calculation["rate"])
                text += f" ({final} − {base}) / {base} × 100 = {rate}{percent}."
    return text
