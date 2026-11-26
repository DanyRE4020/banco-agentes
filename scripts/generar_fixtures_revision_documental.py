"""Ejemplos ficticios y genéricos, sin reproducir el diseño de una credencial oficial."""
import json
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1] / "fixtures/documents/revision"


def field(value, label=None):
    return {"valor": value, "confianza": .99, "pagina": 1, "lectura": "leido",
            "etiqueta": label, "texto_original": value}


def examples():
    return {
        "ine_ficticia.pdf": ("INE FICTICIA - SIN VALIDEZ\nNombre: Persona Ejemplo Alfa\nVigencia: 2010 - 2020\nDomicilio: Calle Inventada 10",
            {"tipo": "identificacion", "legible": True, "campos": {
                "clase_identificacion": field("INE"), "nombre": field("Persona Ejemplo Alfa"),
                "fecha_vencimiento": {**field("2020", "Vigencia"), "texto_original": "2010 - 2020"},
                "domicilio": field("Calle Inventada 10")}}),
        "recibo_servicio.png": ("RECIBO DE SERVICIOS FICTICIO\nServicio: electricidad\nTitular: Persona Ejemplo Beta\nDomicilio del servicio: Calle Inventada 20\nLimite de pago: 2020-01-15\nTotal: 250.00 MXN",
            {"tipo": "comprobante", "legible": True, "campos": {
                "tipo_comprobante": field("electricidad"), "servicio_descripcion": field("electricidad"),
                "titular": field("Persona Ejemplo Beta"), "domicilio_servicio": field("Calle Inventada 20"),
                "fecha_limite_pago": field("2020-01-15", "Límite de pago"), "importe_total": field("250.00", "Total")}}),
        "cfdi_pago.jpg": ("CFDI DE NOMINA FICTICIO\nTitular: Persona Ejemplo Gamma\nPeriodo: primera quincena de enero 2020\nNeto a pagar: 150.00 MXN",
            {"tipo": "cfdi_nomina", "legible": True, "campos": {
                "titular": field("Persona Ejemplo Gamma"), "periodo_documento": field("primera quincena de enero 2020", "Periodo"),
                "neto_a_pagar": field("150.00", "Neto a pagar"), "moneda": field("MXN")}}),
    }


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    expected = {}
    for name, (text, raw) in examples().items():
        raw["paginas"] = [{"pagina": 1, "texto": "DOCUMENTO SINTETICO - SIN VALIDEZ\n\n" + text,
                           "estado": "leida"}]
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_textbox(fitz.Rect(45, 45, 550, 760), "DOCUMENTO SINTETICO - SIN VALIDEZ\n\n" + text,
                                fontsize=14)
            if name.endswith(".pdf"):
                pdf.save(ROOT/name)
                page.get_pixmap().save(ROOT/"ine_ficticia.png")
                expected["ine_ficticia.png"] = raw
            else:
                page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4)).save(ROOT/name)
        expected[name] = raw
    (ROOT/"expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
