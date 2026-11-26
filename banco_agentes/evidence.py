"""Vista de evidencia para el modelo, sin repetir DDL ni definiciones vacías."""

import copy
from .catalog import normalize


def compact_evidence(result):
    result = copy.deepcopy(result)
    query = normalize(result["query"])
    structural = ("columnas" in query or "clave primaria" in query) and not any(
        word in query for word in ("signific", "regla", "relacion", "join", "negocio", "explica"))
    if structural or result["status"] == "not_found":
        result["documentation"] = []
        result["interpretations"] = []
        result["scope"] = "DDL para columnas/claves o comprobación de ausencia; comentarios generales y relaciones no solicitadas omitidos."
    result["searched_sources"] = [{"file": source["file"]} for source in result["searched_sources"]]
    for table in result["tables"]:
        table["source"].pop("excerpt", None)
        table["columns"] = [{"declaration": column["declaration"],
                             "line": column["source"]["line"]}
                            for column in table["columns"]]
    result["column_source_rule"] = "Cada columna cita el archivo source.file de su tabla y su propia line. Las declaraciones son SQL explícito; no equivalen a definiciones de negocio."
    return result
