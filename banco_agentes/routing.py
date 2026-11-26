"""Excepción acotada para conversación social sin consulta adjunta."""

import re
from .catalog import normalize


def requests_real_balance(text: str) -> bool:
    text = normalize(text)
    return bool(re.search(r"\bsaldos?\s+real(?:es)?\b", text)) and not re.search(
        r"\b(?:campo|columna|tabla|catalogo|significa|regla|metadatos)\b", text)


def is_simple_greeting(text: str) -> bool:
    # fullmatch impide tratar «Hola, ¿qué columnas ...?» como saludo aislado.
    text = re.sub(r"[¡!¿?,.;:]", " ", normalize(text))
    text = " ".join(text.split())
    return re.fullmatch(r"(?:hola(?: (?:buenos dias|buenas tardes|buenas noches))?"
                        r"|buenos dias|buenas tardes|buenas noches)", text) is not None
