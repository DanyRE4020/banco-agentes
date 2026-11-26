"""Solo presentación de reconocimientos ya calculados: sin lector, Steward, red o archivos personales."""
import copy
import json
import unittest

from banco_agentes.document_review_presentation import client_view, requirement_items, CLOSING, OPENING


SOURCE = {"title": "Política ficticia", "page": 2, "version": "prueba-1", "consulted_at": "2026-11-18"}


def example():
    documents = [
        {"id":"D1", "tipo":"identificacion", "campos":{}},
        {"id":"D2", "tipo":"comprobante", "campos":{"tipo_comprobante":{"valor":"electricidad", "estado":"confirmado"}}},
        {"id":"D3", "tipo":"cfdi_nomina", "campos":{}},
    ]
    categories = ["identificacion", "domicilio", "ingresos"]
    labels = ["Identificación", "Comprobante de domicilio", "Comprobante de ingresos"]
    recognized = [{"documento_id":f"D{i}", "categoria":key, "estado":"aportada", "datos_pendientes":[],
        "evidencia":[{"campo":"clase_identificacion" if i==1 else "fecha_limite_pago" if i==2 else "neto_a_pagar",
            "etiqueta":"Vigencia" if i==1 else "Límite de pago" if i==2 else "Neto a pagar",
            "fuente":{"archivo":f"ejemplo_{i}.pdf", "pagina":1}}]} for i,key in enumerate(categories,1)]
    rows=[{"categoria":key, "label":label, "status":"aportada", "reason":"Contenido legible: campos internos"}
          for key,label in zip(categories,labels)]
    policy={"producto":"clasica", "categorias_documentales":[{"categoria":c} for c in categories],
        "total_documental_definido":True, "requisitos_recuperados":[{"fuente":SOURCE,"texto":
        "1. Requisitos\n• Identificación oficial vigente.\n• Comprobante de domicilio reciente.\n"
        "• Contar con ingreso mensual comprobable de $7,000.00 MXN.\n• Tener entre 18 y 69 años.\n"
        "2. Costos\nPago mínimo: $300.00."}]}
    return documents,policy,recognized,rows


class ReviewStyleTests(unittest.TestCase):
    def test_natural_names_independent_bullets_grouped_sources_one_closing(self):
        args=example()
        before=copy.deepcopy(args)
        summary,view=client_view(*args,question="Revisa mis documentos para Clásica")
        self.assertEqual(args,before)  # No cambia el reconocimiento ni sus evidencias.
        self.assertTrue(summary.startswith(OPENING))
        self.assertEqual(view["document_names"],["INE","recibo de luz","recibo de nómina"])
        self.assertEqual(view["policy_title"],"Requisitos para solicitar Tarjeta de Crédito Clásica")
        self.assertEqual(len(view["policy_requirements"]),4)
        self.assertTrue(all("\n" not in r["text"] for r in view["policy_requirements"]))
        self.assertIn("Tener al menos 18 años.",[r["text"] for r in view["policy_requirements"]])
        self.assertEqual(summary.count(CLOSING),1)
        self.assertTrue(summary.endswith(CLOSING))
        for private in ("D1","R2","aportada","total documental cerrado","categorías identificadas",
                        "Límite de pago","Neto a pagar","69","300.00"):
            self.assertNotIn(private,summary)
        self.assertNotIn("ejemplo_1.pdf",summary)  # Citas agrupadas, sin interrumpir la respuesta.
        self.assertIn("ejemplo_1.pdf, página 1",view["references"])
        self.assertTrue(any("Política ficticia, página 2" in r and "versión prueba-1" in r for r in view["references"]))
        self.assertTrue(all("reference" not in r for r in view["policy_requirements"]))

    def test_missing_and_unreadable_use_everyday_words_without_field_labels(self):
        docs,policy,recognized,rows=example()
        rows[-1]["status"]="ausente"
        rows[0]["status"]="pendiente"
        recognized[0]["datos_pendientes"]=["fecha_vencimiento"]
        summary,view=client_view(docs[:-1],policy,recognized[:-1],rows)
        self.assertIn("Falta subir: Comprobante de ingresos.",summary)
        self.assertIn("No pude completar la lectura de: Identificación.",summary)
        self.assertNotIn("fecha_vencimiento",json.dumps(view))
        self.assertNotIn("aportada",json.dumps(view))

    def test_general_style_for_other_products_and_document_types(self):
        for product,name in (("nomina","Crédito Nómina"),("cuenta_universitaria","Cuenta Universitaria"),("general","Cuenta Universitaria")):
            with self.subTest(product=product):
                docs,policy,recognized,rows=example()
                policy.update(producto=product,nombre_producto=name)
                if product == "general":
                    policy.pop("nombre_producto")
                    policy["requisitos_recuperados"][0]["fuente"] = {**SOURCE, "title":name}
                docs[1]["campos"]["tipo_comprobante"]["valor"]="agua"
                docs[2]["tipo"]="estado_de_cuenta"
                _,view=client_view(docs,policy,recognized,rows)
                self.assertEqual(view["policy_title"],"Requisitos para solicitar "+name)
                self.assertEqual(view["document_names"],["INE","recibo de agua","estado de cuenta"])

    def test_unrelated_payment_and_maximum_age_hidden_but_explicit_questions_keep_them(self):
        text="Tener entre 18 y 69 años. Pago mínimo: $300.00. Presentar identificación vigente."
        default=requirement_items(text,"Revisa mis documentos")
        explicit=requirement_items(text,"¿Qué edad máxima y pago mínimo se piden?")
        self.assertEqual(len(default),2)
        self.assertFalse(any("69" in r or "300.00" in r for r in default))
        self.assertEqual(len(explicit),3)
        self.assertTrue(any("69" in r for r in explicit))
        self.assertTrue(any("300.00" in r for r in explicit))

    def test_structured_policy_has_natural_requirements_and_deduplicates(self):
        docs,policy,recognized,rows=example()
        policy.pop("requisitos_recuperados")
        policy["reglas"]=[{"campo":"edad_anios","operador":">=","valor":18,"fuente":SOURCE},
            {"campo":"edad_anios","operador":"<=","valor":69,"fuente":SOURCE},
            {"campo":"identificacion_oficial","fuente":SOURCE},
            {"campo":"vigencia_identificacion","fuente":SOURCE}]
        _,view=client_view(docs,policy,recognized,rows)
        self.assertEqual([r["text"] for r in view["policy_requirements"]],
                         ["Tener al menos 18 años.","Presentar una identificación oficial vigente."])
        self.assertEqual(sum("Política ficticia" in r for r in view["references"]),1)

    def test_duplicate_recognized_files_do_not_repeat_natural_name(self):
        docs,policy,recognized,rows=example()
        duplicate=copy.deepcopy(docs[0]); duplicate["id"]="D4"
        evidence=copy.deepcopy(recognized[0]); evidence["documento_id"]="D4"
        evidence["evidencia"][0]["fuente"]["archivo"]="otra_identificacion.png"
        _,view=client_view(docs+[duplicate],policy,recognized+[evidence],rows)
        self.assertEqual(view["document_names"].count("INE"),1)
        self.assertIn("otra_identificacion.png, página 1",view["references"])

    def test_document_requirements_in_one_sentence_become_individual_items(self):
        items=requirement_items("Documentos solicitados: identificación oficial, comprobante de domicilio (luz, agua y recibo de gas) y comprobante de ingresos.", "Revisa mis documentos")
        self.assertEqual(items,["Identificación oficial.", "Comprobante de domicilio (luz, agua y recibo de gas).", "Comprobante de ingresos."])

    def test_explicit_answer_when_none_missing_and_pending_reading_is_not_absence(self):
        docs,policy,recognized,rows=example()
        summary,_=client_view(docs,policy,recognized,rows)
        self.assertIn("No falta ninguno de los tipos de documentos solicitados",summary)
        rows[0]["status"]="pendiente"
        summary,_=client_view(docs,policy,recognized,rows)
        self.assertIn("No falta ninguno",summary)
        self.assertIn("No pude completar la lectura de: Identificación",summary)
        self.assertNotIn("Falta subir",summary)
        # Con archivo sin reconocer no se afirma que esté completo ni que falte con certeza.
        summary,_=client_view(docs[1:],policy,recognized[1:],rows)
        self.assertIn("No pude determinar si faltan: Identificación",summary)
        self.assertNotIn("No falta ninguno",summary)

    def test_missing_policy_list_does_not_invent_a_complete_document_set(self):
        docs,policy,recognized,rows=example()
        policy.pop("categorias_documentales")
        summary,_=client_view(docs,policy,recognized,rows)
        self.assertIn("No pude determinar si falta algún tipo de documento",summary)
        self.assertNotIn("No falta ninguno",summary)

    def test_only_relevant_requirements_keep_their_page_citations(self):
        docs,policy,recognized,rows=example()
        excluded="Este documento no indica cómo se calcula el pago mínimo."
        policy["requisitos_recuperados"]=[
            {"fuente":{**SOURCE,"page":9},"texto":excluded + " Disfruta promociones exclusivas."},
            {"fuente":{**SOURCE,"page":2},"texto":"Presentar identificación oficial vigente."},
            {"fuente":{**SOURCE,"page":4},"texto":"Comprobante de domicilio reciente."},
            {"fuente":{**SOURCE,"page":6},"texto":"Presentar identificación oficial vigente."},
        ]
        before=copy.deepcopy(policy)
        summary,view=client_view(docs,policy,recognized,rows,question="Revisa mis documentos para Clásica")
        self.assertEqual(policy,before)  # El texto recuperado permanece íntegro internamente.
        self.assertNotIn("Este documento no indica cómo se calcula",summary)
        self.assertNotIn("promociones",summary)
        self.assertFalse(any("página 9" in r for r in view["references"]))
        self.assertEqual(len(view["policy_requirements"]),2)
        for requirement,pages in zip(view["policy_requirements"],((2,6),(4,))):
            references=[view["references"][index-1] for index in requirement["source_indices"]]
            self.assertEqual(len(references),len(pages))
            for page in pages:
                self.assertTrue(any(f"página {page}" in r for r in references))
        self.assertEqual(requirement_items("Este resumen no detalla el cálculo de comisiones.","Revisa mis documentos"),[])
