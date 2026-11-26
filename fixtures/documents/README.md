# Piloto de documentos sintéticos

Generar: `.\.venv\Scripts\python.exe -B scripts/generar_documentos_sinteticos.py`.
El manifiesto conserva los nombres de los ocho fixtures originales; ya no limita los contenidos admitidos.
La subida valida formato real PDF/PNG/JPG, tamaño, páginas y permisos. No exige casilla ni parámetro `synthetic`.
El formulario informa que los adjuntos seleccionados se envían a Gemini para su lectura.

- `estado_cuenta.pdf`: octubre de 2026 completo, importes MXN y domicilio ficticio.
- `identificacion_vigente.png` / `identificacion_vencida.png`: la única diferencia visual es el vencimiento; también hay versiones JPG.
- `identificacion_ilegible.png`: vencimiento explícitamente ilegible para probar la corrección con procedencia propia.
- `no_bancario.pdf`: documento legible de jardinería, con texto de inyección deliberado.
- `estado_instrucciones.pdf`: el mismo estado con instrucciones adversariales incrustadas como datos.

El chat permite seleccionar los adjuntos que usa cada turno; desmarca la identificación anterior al comparar con la otra.
Los archivos de subida se guardan en `local_uploads/`, fuera de los recursos públicos e ignorados por Git.
Los metadatos y la caché del primer corte son locales en memoria, por lo que caducan al reiniciar el servidor.
La cuota de seis archivos distintos (por hash de contenido) se aplica a la identidad del servidor mientras vive este almacén.
Reutilizar un fixture en otra conversación no consume otra vez la cuota; cada conversación conserva sus propios IDs y autorización.
No se permite repetir el mismo contenido dentro de una conversación. No hay borrado/retención automático en este corte.

Gemini recibe únicamente los archivos de prueba seleccionados. El lector usa la credencial ya presente en el proceso y `BANCO_DOCUMENT_MODEL`
(por defecto `gemini-3.5-flash-lite`); no carga archivos `.env`.
La llamada es única por subida, sin reintentos automáticos. Ante un fallo, abre otra conversación y vuelve a adjuntar el fixture.

Prueba real del lector: `.\.venv\Scripts\python.exe -B scripts/verify_document_reader.py`.
El hub y la política de ese script son dobles locales; verifica únicamente el lector real, sin RAG/pgvector.
Servidor para revisar la interfaz con dobles, sin Gemini/BD: `.\.venv\Scripts\python.exe -B scripts/verify_document_ui.py`.
Abre `http://127.0.0.1:8017`, inicia sesión con la cuenta ficticia y adjunta los fixtures.

La comparación es una referencia preliminar calculada por código. La ficha genérica no acredita identificación oficial,
los abonos solo estiman ingresos y el estado de cuenta no sustituye los tipos de comprobante exigidos por la política.

Adjuntos de contenido general: `.\.venv\Scripts\python.exe -B scripts/generar_adjuntos_genericos.py`.
En `genericos/`: contrato bancario PDF con cláusula e instrucción adversarial, comprobante PNG/JPG,
captura ficticia del clima y PDF válido en blanco (ilegible). Todos son sintéticos; el blanco lleva la marca en metadatos.
El tipo detectado no decide el alcance: el hub evalúa la pregunta y la evidencia estructurada, cita archivo/página
y consulta a Steward cuando necesita políticas o requisitos. La categoría genérica es `desconocido`.
Un adjunto irrelevante no bloquea la parte bancaria que pueda responderse con política. Una pregunta ajena al banco
no llama a spokes; un error de lectura o proveedor se informa por separado. No se usa evidencia de baja confianza.

Prueba real nueva (lector + hub, y Steward local si hay credencial en el proceso):
`.\.venv\Scripts\python.exe -B scripts/verify_general_attachments.py`.
No abre archivos de secretos y guarda solo IDs, métricas y errores técnicos en `verification-general-attachments-real.json`.
Prueba del front con DOM/fetch dobles: `node tests/check_general_attachments_front.cjs`.

Evidencia por requisito: `.\.venv\Scripts\python.exe -B scripts/generar_fixtures_evidencia.py`.
En `evidencia/` hay siete fixtures PDF/PNG/JPG, con emisores ficticios, nombres reordenados o distintos,
fechas con precisión de día/mes/año y un PDF vacío. Los documentos significativos llevan la marca
DOCUMENTO SINTETICO y SIN VALIDEZ; no imitan una credencial oficial. `expected.json` alimenta los dobles,
no es una salida de Gemini. Los PNG de vista previa permiten revisar los PDFs localmente.
La matriz de regresión está en `tests/test_document_evidence.py`, con fecha de evaluación fija y ambos perfiles.
Piloto real, exclusivamente con dos de estos fixtures sintéticos:
`.\.venv\Scripts\python.exe -B scripts/verify_document_evidence.py --base-url http://127.0.0.1:8004`.
Separa lectura de un campo de evaluación de requisitos; conserva solo IDs, códigos de estado y métricas.
El tipo oficial no se autentica; el total del comprobante no se usa como ingreso. Las discrepancias nominales
afectan únicamente requisitos que vinculan documentos, sin anular edad/vigencia de la identificación legible.

Lectura completa: `.\.venv\Scripts\python.exe -B scripts/generar_fixtures_lectura_completa.py`.
En `lectura_completa/`: PDF digital de tres páginas, su equivalente escaneado, PNG, JPG y una imagen
con domicilio tapado. Todos son documentos genéricos ficticios marcados SIN VALIDEZ. El domicilio y
un campo fuera del esquema habitual están en la última página; también hay fecha textual, importe
con formato original, datos personales que no deben acompañar otras respuestas y texto adversarial.
`transcripciones_dobles.json` alimenta exclusivamente los dobles de pruebas; no es una salida de Gemini.

El lector conserva texto privado por página: PyMuPDF para texto seleccionable y Gemini para páginas visuales.
Solo el fragmento pedido se presenta con su referencia. Consultas de lectura directa no llaman a Steward
ni al hub generativo. Una etiqueta clara se resuelve localmente; el selector aislado se usa para localizar
fragmentos narrativos y valida citas literales contra la página, sin herramientas ni instrucciones del archivo.
El texto completo nunca se devuelve en la API, historial del hub, logs o telemetría.
Se conservan los límites de subida (5 MiB, veinte páginas, seis archivos), permisos y selección del turno.
Límite de contenido: 20.000 caracteres por página y 120.000 por archivo. Al alcanzarlo se marca lectura parcial
con páginas incompletas/sin revisar; páginas omitidas por OCR y errores del proveedor no se confunden con ausencia.
Un marcador de texto ilegible fuerza estado parcial aunque el proveedor marque la página como leída.

Pruebas sin red: `tests/test_document_content.py`, ambos perfiles y diez pruebas (incluyen PDF mixto/fallo del proveedor).
Piloto real: `.\.venv\Scripts\python.exe -B scripts/verify_full_document_reading.py --base-url http://127.0.0.1:8004`.
Caso individual: añadir `--fixture ilegible.png` (también admite los otros nombres del conjunto).
Los informes `verification-full-reading-real*.json` guardan comprobaciones, cobertura, IDs y métricas, sin transcripciones.

Ingreso por CFDI y domicilio: `.\.venv\Scripts\python.exe -B scripts/generar_fixtures_ingreso_domicilio.py`.
En `ingreso_domicilio/` hay dos CFDI mensuales, su variante con anexo ilegible y recibos de servicios
antiguo/reciente/no admitido/mes en el límite/paquete sin identificar servicio. Todos son ficticios y sin validez.
Se reutiliza la identificación genérica de `evidencia/` para las pruebas conjuntas, sin imitar una INE oficial.
Los importes esperados y DOC-11 se usan solo como referencias de pruebas; producción busca por producto/requisito.
`expected.json` es un doble, no una extracción de Gemini. Los campos conservan etiqueta, valor original y página;
domicilio del servicio y fiscal, expedición/emisión, facturación, límite de pago y corte están separados.

Suite: `tests/test_income_proof.py`, lector/hub/recuperación dobles, ambos perfiles en la recuperación parcial.
Piloto real: `.\.venv\Scripts\python.exe -B scripts/verify_income_proof.py --base-url http://127.0.0.1:8004`.
Para repetir solo un caso: añadir `--case partial`, `--case joint`, `--case below` o `--case old_service`.
La salida guarda IDs, estados, fuentes de política y métricas, sin texto completo ni valores personales.
Si falla Gemini, no hay reintentos automáticos: la app conserva los campos recuperables del texto local y avisa
qué página quedó pendiente. Un fallo sin evidencia legible no se convierte en una afirmación de ausencia.
