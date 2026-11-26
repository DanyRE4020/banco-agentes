Ejemplos enteramente ficticios, sin diseño ni logos de una credencial oficial.
Generar con ` .\.venv\Scripts\python.exe -B scripts/generar_fixtures_revision_documental.py` desde la raíz.

- `ine_ficticia.pdf` / `ine_ficticia.png`: mismo ejemplo en dos formatos. Declara INE ficticia y un rango impreso pasado.
- `recibo_servicio.png`: recibo genérico de electricidad, domicilio del servicio y límite de pago antiguo.
- `cfdi_pago.jpg`: CFDI ficticio, importe neto bajo y periodo quincenal impreso.
- `expected.json`: extracción doble de esos ejemplos, exclusivamente para pruebas automáticas sin red.

Titulares distintos, fechas antiguas e importe bajo son deliberados: este corte solo reconoce documentación legible,
no determina elegibilidad. La categoría se cuenta una vez aunque se adjunten los dos formatos de la identificación.
Los nombres originales y los IDs no deciden la clasificación; las pruebas suben nombres distintos.

La política ficticia de las pruebas declara explícitamente tres categorías y una lista cerrada.
Las políticas recuperadas sin ese cierre no deben producir una fracción. La prueba automática es:
`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_document_review.py -v`.

Para prueba manual con Gemini: iniciar la aplicación, abrir un chat nuevo y adjuntar PDF, recibo y CFDI.
Pedir «Revisa mi documentación para solicitar la Tarjeta Clásica». Repetir sin CFDI y con los dos formatos de INE.
Preguntar después «¿Qué domicilio aparece en esta identificación?» conserva el dato y la referencia a la página.
No se requieren documentos reales para reproducir los casos.
