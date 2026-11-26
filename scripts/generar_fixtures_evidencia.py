"""Fixtures genéricos sintéticos PDF/PNG/JPG. Sin logotipos ni imitación de credenciales oficiales."""
import copy
import json
from pathlib import Path

import fitz

OUTPUT = Path(__file__).resolve().parent.parent/"fixtures/documents/evidencia"


def field(value, *, reading="leido"):
    return {"valor": value, "pagina": 1, "confianza": .99 if value is not None else .1,
            "lectura": reading if value is not None or reading == "ilegible" else "ausente"}


def samples():
    identity = {"tipo": "identificacion", "legible": True, "campos": {k: field(v) for k, v in {
        "nombre": "Lucía Marina Rojas Torres", "fecha_nacimiento": "1990-05-20", "fecha_vencimiento": "2032",
        "domicilio": "Calle Ensayo 42, Ciudad Ejemplo", "tipo_identificacion": "no_oficial",
        "emisor": "Registro Vecinal Aurora", "numero_enmascarado": "****1234"}.items()}}
    receipt = {"tipo": "comprobante", "legible": True, "campos": {k: field(v) for k, v in {
        "titular": "ROJAS TORRES LUCIA MARINA", "fecha_emision": "2026-10", "fecha_vencimiento": "2026-12-15",
        "domicilio": "Calle Ensayo 42, Ciudad Ejemplo", "tipo_comprobante": "electricidad",
        "emisor": "Lumen Delta", "importe_total": "230.00", "moneda": "MXN"}.items()}}
    result = {"registro_personas.pdf": identity, "comprobante_municipal.pdf": receipt,
              "tarjeta_vecinal.png": copy.deepcopy(identity), "ficha_registro.jpg": copy.deepcopy(identity),
              "documento_servicio.jpg": copy.deepcopy(receipt), "pago_servicio.png": copy.deepcopy(receipt),
              "archivo_vacio.pdf": {"tipo": "desconocido", "legible": False, "campos": {}}}
    result["tarjeta_vecinal.png"]["campos"]["emisor"] = field("Asociación Horizonte")
    result["documento_servicio.jpg"]["campos"].update(emisor=field("Agua Nube"), tipo_comprobante=field("agua"), fecha_emision=field("2026-08"))
    result["pago_servicio.png"]["campos"].update(emisor=field("Servicios Viento"), tipo_comprobante=field("gas"),
        titular=field("Rafael Andrés León Soto"), fecha_emision=field("2026-10-04"))
    return result


def generate(output=OUTPUT):
    output.mkdir(parents=True, exist_ok=True)
    data = samples()
    for name, raw in data.items():
        with fitz.open() as document:
            page = document.new_page(width=700, height=800)
            if raw["legible"]:
                page.insert_text((28, 45), "DOCUMENTO SINTETICO - SIN VALIDEZ", fontsize=18, color=(.7, .1, .1))
                page.insert_text((28, 78), raw["tipo"].replace("_", " "), fontsize=16)
                for i, (key, value) in enumerate(raw["campos"].items()):
                    page.insert_text((28, 120+38*i), key.replace("_", " ")+": "+str(value["valor"]), fontsize=12)
                page.insert_text((28, 750), "Ejemplo academico. No es una credencial ni recibo oficial.", fontsize=11)
            document.set_metadata({"title": "DOCUMENTO SINTETICO - Sin validez"})
            if name.endswith(".pdf"):
                document.save(output/name, no_new_id=True)
                if raw["legible"]:
                    page.get_pixmap().save(output/(name+".preview.png"))
            else:
                page.get_pixmap().save(output/name)
    (output/"expected.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    return data


if __name__ == "__main__":
    print(f"Fixtures sintéticos: {len(generate())} en {OUTPUT}")
