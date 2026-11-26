# Configuración de los seis evaluadores en LangSmith

El runner publica evidencia. La calificación pertenece exclusivamente a la UI.
Se conservan las preguntas y referencias de `evaluacion_banco_agentes.csv` y los
prompts de los agentes. Los evaluadores Python anteriores quedan para pruebas de
desarrollo: el runner no los importa, no calcula notas ni los registra en el SDK.

## Configuración en la interfaz

En **Datasets & Experiments**, abre `evaluacion-data-agentes`, entra en
**Evaluators** y agrega los seis evaluadores, cada uno como **LLM-as-a-Judge**.
Selecciona el modelo del juez en la UI. En cada evaluador pega el archivo `.txt`
completo indicado abajo y configura un solo feedback de tipo **Categorical**.
Las instrucciones comunes ya están incluidas en cada archivo.

| Feedback key y nombre | Prompt completo | Categorías |
|---|---|---|
| `enrutamiento_correcto` | [enrutamiento_correcto.txt](enrutamiento_correcto.txt) | correcto, incorrecto, no_aplica, no_verificable |
| `exactitud_numerica` | [exactitud_numerica.txt](exactitud_numerica.txt) | correcto, incorrecto, no_aplica, no_verificable |
| `responde_la_pregunta` | [responde_la_pregunta.txt](responde_la_pregunta.txt) | correcto, incorrecto, no_aplica, no_verificable |
| `responde_correcto` | [responde_correcto.txt](responde_correcto.txt) | correcto, incorrecto, no_aplica, no_verificable |
| `manejo_de_fechas` | [manejo_de_fechas.txt](manejo_de_fechas.txt) | correcto, incorrecto, no_aplica, no_verificable |
| `alucinacion` | [alucinacion.txt](alucinacion.txt) | detectada, no_detectada, no_aplica, no_verificable |

Usa el formato de plantilla **Mustache** y mapea la única variable de los seis
prompts de la misma manera:

| Variable del prompt | Origen en el selector de LangSmith | Campo |
|---|---|---|
| `{{contexto}}` | **Output** del run | `contexto_evaluacion` |

`contexto_evaluacion` es una cadena JSON preparada por el runner. Tiene tanto la
respuesta real como las rúbricas originales; no depende de que la importación
del CSV haya colocado cada referencia en inputs o en reference outputs. El runner
comprueba que los ejemplos remotos coincidan con el CSV antes de publicar y no
crea ni actualiza ejemplos.

Guarda una sola vinculación de cada criterio al dataset. Estos evaluadores se
aplican a experimentos nuevos; configura los seis antes de la corrida que quieras
calificar. Si el selector todavía no muestra el campo nuevo, una corrida inicial
de captura permite disponer de una salida de ejemplo: configura después el mapeo
y crea un experimento nuevo. La captura inicial queda sin calificar; evita volver
a ejecutar manualmente los mismos jueces sobre runs ya calificados.

LangSmith documenta el [mapeo y feedback categórico](https://docs.langchain.com/langsmith/llm-as-judge)
y la [vinculación al dataset](https://docs.langchain.com/langsmith/bind-evaluator-to-dataset).
El manifiesto [configuracion_ui.json](configuracion_ui.json) resume esta
configuración manual; el script no instala evaluadores en tu cuenta.

## Contenido del campo publicado

Las rutas siguientes están dentro del JSON `contexto_evaluacion`. Son el mapeo
detallado para revisar el preview y redactar otros jueces en la UI.

| Campo del contexto | Procedencia y uso |
|---|---|
| `id_caso`, `categoria`, `perfil` | Columnas originales del CSV; identidad del ejemplo, no instrucciones para cambiar la sesión |
| `pregunta` | Texto exacto de la pregunta final |
| `turnos_previos` | Mensajes originales de `turnos_previos_json`, en orden; recuperan alcance, no aportan una respuesta final |
| `respuesta_visible.texto` | `final.response.answer` del último turno |
| `respuesta_visible.formato` | Formato seleccionado que utiliza la web |
| `respuesta_visible.tablas` | Tablas que la web presenta en ese formato |
| `respuesta_visible.graficas` | Gráficas visibles, con puntos, período, unidad, citas e IDs; su tabla accesible usa esos mismos puntos |
| `respuesta_visible.fuentes` | Fuentes que el usuario puede desplegar al final |
| `estado_ejecucion` | Resultado técnico: ok, fallo_api, historial_incompleto o fallo_tecnico; no es una calificación |
| `correlacion` | Método, estado y trace_id si existe; no se selecciona una traza por ser la última |
| `traza_ultimo_turno` | Solo la traza correlacionada de ese turno, o null si no se puede identificar inequívocamente |
| `referencia_sql.status` | ok, no_aplica o no_verificable para la disponibilidad del oráculo; tampoco es una nota |
| `referencia_sql.reader_role_verified`, `transaction_read_only` | Prueba de rol de lectura y transacción de solo lectura |
| `referencia_sql.query_file`, `query_sha256`, `parameters` | Identificación del SELECT revisado y sus parámetros, sin credenciales |
| `referencia_sql.facts[]` | Producto, mes, métrica, valor y segmento cuando aplica, calculados directamente en PostgreSQL |
| `referencia_sql.derived` | Diferencia/mayor de dos saldos; crecimiento absoluto y porcentual por producto; máximo de una serie |
| `referencia_sql.derived.growth_winners` | Ganadores calculados por código para ambas definiciones de crecimiento en BA-012 |
| `referencias_dataset.enrutamiento_esperado` | Referencia original, incluida la lista con `\|` o solo_orquestador |
| `referencias_dataset.exactitud_numerica` | Rúbrica numérica original |
| `referencias_dataset.responde_la_pregunta` | Rúbrica original de cobertura |
| `referencias_dataset.responde_correcto` | Condición original de contenido y corrección |
| `referencias_dataset.manejo_de_fechas` | Alcance temporal original |
| `referencias_dataset.alucinacion` | Rúbrica original de sustento de afirmaciones |
| `fecha_referencia_dataset`, `zona_horaria_referencia` | 2026-11-12 y America/Mexico_City; no se envían al agente |
| `naturaleza_datos`, `tolerancias_de_presentacion` | Datos sintéticos sin moneda; tolerancias para presentación numérica |
| `calificacion_python_habilitada` | Siempre false en este flujo |

El contexto de los jueces excluye respuestas de turnos previos, cabeceras,
contraseñas, cookies y tokens. Los diagnósticos plegados (`query_details`) no se
promueven a respuesta visible. La traza y las referencias son evidencia de
verificación, no prueba de que el usuario haya visto esos valores.

## Repetir los tres casos y los 21

Desde la carpeta del proyecto, con la web en `http://127.0.0.1:8001` y
`LANGSMITH_API_KEY` configurada en **esa consola**:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-evaluation.txt
.\.venv\Scripts\python.exe .\scripts\evaluar_langsmith.py --cases BA-006 BA-002 BA-012 --base-url http://127.0.0.1:8001 --dataset evaluacion-data-agentes --publish
```

Para los 21:

```powershell
.\.venv\Scripts\python.exe .\scripts\evaluar_langsmith.py --all --base-url http://127.0.0.1:8001 --dataset evaluacion-data-agentes --publish
```

Si necesitas iniciar la web, usa otra consola:

```powershell
$env:BANCO_HUB_MODEL = 'gemini-3.5-flash-lite'
$env:BANCO_SPOKES_MODEL = 'gemini-3.5-flash-lite'
.\.venv\Scripts\python.exe -m banco_agentes.web --provider gemini --port 8001
```

El runner lee la clave de LangSmith solo del entorno. La conexión SQL utiliza el
mecanismo habitual de lectura de la aplicación y comprueba el rol; el revisor no
abre los archivos de secretos. Para otro archivo de auditoría, especifica
`--audit-log`. La publicación usa `evaluators=[]`, `summary_evaluators=[]` y una
repetición por caso. Los jueces vinculados en la UI pueden terminar después de
que el script haya publicado la evidencia: el resultado del SDK no certifica que
la calificación de la UI haya terminado. Revisa esos seis feedbacks en el
experimento. Los reportes locales `evaluation-results-ui-*.json` no contienen
puntajes Python.
