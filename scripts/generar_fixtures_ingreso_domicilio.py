"""CFDI y recibos ficticios para los dos requisitos; no documentos oficiales ni datos reales."""
import copy
import json
from pathlib import Path

import fitz

OUTPUT = Path(__file__).resolve().parent.parent/"fixtures/documents/ingreso_domicilio"


def f(value, label):
    return {"valor": value, "texto_original": value, "etiqueta": label, "pagina": 1, "confianza": .99, "lectura": "leido"}


def samples():
    payroll = {"tipo": "cfdi_nomina", "legible": True, "campos": {k: f(v, label) for k, label, v in (
        ("titular", "Titular", "Lucía Marina Rojas Torres"), ("periodicidad", "Periodicidad", "mensual"),
        ("periodo_inicio", "Inicio del periodo", "2026-10-01"), ("periodo_fin", "Fin del periodo", "2026-10-31"),
        ("moneda", "Moneda", "MXN"), ("concepto_pago", "Concepto", "Apoyo económico"),
        ("neto_a_pagar", "Neto a pagar", "12000.00"), ("percepciones_brutas", "Percepciones brutas", "15000.00"),
        ("deducciones", "Deducciones", "3000.00"))}}
    receipt = {"tipo": "comprobante", "legible": True, "campos": {k: f(v, label) for k, label, v in (
        ("titular", "Titular", "ROJAS TORRES LUCIA MARINA"), ("tipo_comprobante", "Servicio", "electricidad"),
        ("servicio_descripcion", "Descripción del servicio", "Energía eléctrica"),
        ("domicilio_servicio", "Domicilio del servicio", "Calle Ensayo 42, Ciudad Ejemplo"),
        ("domicilio_fiscal", "Domicilio fiscal", "Avenida Ficticia 900, Otra Ciudad"),
        ("mes_facturado", "Mes facturado", "2025-01"), ("fecha_limite_pago", "Límite de pago", "2025-02-20"),
        ("fecha_corte", "Corte", "2025-01-31"), ("importe_total", "Total a pagar", "1800.00"), ("moneda", "Moneda", "MXN"))}}
    result = {"cfdi_mensual.pdf": payroll, "cfdi_bajo.pdf": copy.deepcopy(payroll), "cfdi_parcial.pdf": copy.deepcopy(payroll),
        "luz_antigua.pdf": receipt, "factura_reciente.pdf": copy.deepcopy(receipt), "servicio_no_admitido.png": copy.deepcopy(receipt),
        "mes_limite.pdf": copy.deepcopy(receipt), "paquete.pdf": copy.deepcopy(receipt)}
    result["cfdi_bajo.pdf"]["campos"].update(neto_a_pagar=f("6500.00", "Neto a pagar"), percepciones_brutas=f("8000.00", "Percepciones brutas"), deducciones=f("1500.00", "Deducciones"))
    recent = result["factura_reciente.pdf"]["campos"]
    recent.update(tipo_comprobante=f("agua", "Servicio"), servicio_descripcion=f("Suministro de agua", "Descripción del servicio"),
        fecha_expedicion=f("2026-11-01", "Fecha de expedición"), mes_facturado=f("2026-10", "Mes facturado"),
        fecha_limite_pago=f("2026-12-01", "Límite de pago"), fecha_corte=f("2026-11-10", "Corte"))
    result["mes_limite.pdf"]["campos"]["mes_facturado"] = f("2026-08", "Mes facturado")
    result["servicio_no_admitido.png"]["campos"].update(tipo_comprobante=f("servicio_digital", "Servicio"),
        servicio_descripcion=f("Suscripción de videojuegos", "Descripción del servicio"), fecha_emision=f("2026-11-01", "Fecha de emisión"))
    result["paquete.pdf"]["campos"].update(tipo_comprobante=f("no_determinable", "Servicio"),
        servicio_descripcion=f("Paquete integral", "Descripción del servicio"), fecha_emision=f("2026-11-01", "Fecha de emisión"))
    return result


def generate():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = samples()
    for name, raw in data.items():
        with fitz.open() as doc:
            page = doc.new_page(width=720, height=850)
            page.insert_text((30, 40), "DOCUMENTO SINTETICO - SIN VALIDEZ", fontsize=17)
            title = "CFDI de nomina de prueba" if raw["tipo"] == "cfdi_nomina" else "Estado de cuenta" if name == "luz_antigua.pdf" else "Factura de servicios ficticia"
            page.insert_text((30, 76), title, fontsize=16)
            for i, field in enumerate(raw["campos"].values()):
                page.insert_text((30, 115+32*i), field["etiqueta"]+": "+field["texto_original"], fontsize=12)
            if name == "cfdi_parcial.pdf":
                with fitz.open() as source:
                    source.new_page(width=720, height=850).draw_rect(fitz.Rect(10,10,710,840),color=(0,0,0),fill=(0,0,0))
                    image = source[0].get_pixmap().tobytes("png")
                doc.new_page(width=720,height=850).insert_image(fitz.Rect(0,0,720,850),stream=image)
                raw["paginas"] = [{"pagina":1,"texto":"\n".join(v["etiqueta"]+": "+v["texto_original"] for v in raw["campos"].values()),"estado":"leida"},
                    {"pagina":2,"texto":"[[ILEGIBLE]]","estado":"ilegible"}]
            doc.set_metadata({"title":"DOCUMENTO SINTETICO - SIN VALIDEZ"})
            if name.endswith("pdf"):
                doc.save(OUTPUT/name,deflate=True,no_new_id=True)
            else:
                page.get_pixmap().save(OUTPUT/name)
    (OUTPUT/"expected.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    return data


if __name__ == "__main__":
    print("Fixtures sintéticos:",len(generate()))
