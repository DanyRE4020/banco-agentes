"""Usuarios ficticios inyectados por las pruebas. La aplicación no carga este módulo."""
from banco_agentes.auth import password_record
from banco_agentes.web import create_app as production_app

ACCOUNTS = {"cliente": ("ClienteDemo!2026", "Cliente"), "analista": ("AnalistaDemo!2026", "Analista")}
USERS = {
    "cliente": {"rol": "Cliente", "num_cte": 1, **password_record(ACCOUNTS["cliente"][0])},
    "analista": {"rol": "Empleado", "num_cte": None, **password_record(ACCOUNTS["analista"][0])},
}


def create_app(*args, **kwargs):
    kwargs.setdefault("users", USERS)
    return production_app(*args, **kwargs)
