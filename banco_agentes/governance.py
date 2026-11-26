"""Política de demo, independiente del modelo. La identidad nunca se toma del texto."""

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass

from .catalog import ROOT, normalize, requested_definition_fields
from .semantics import CUSTOMER_COMPLETENESS_DETAIL


@dataclass(frozen=True)
class DemoSession:
    profile: str = "Analista"
    customer_id: int | None = None
    account_id: int | None = None
    principal: str | None = None

    def __post_init__(self):
        if self.profile not in {"Cliente", "Analista"}:
            raise ValueError("Perfil de demo desconocido.")
        if self.profile == "Cliente" and (type(self.customer_id) is not int or self.customer_id <= 0):
            raise ValueError("Cliente requiere identidad configurada fuera del mensaje.")

    @property
    def num_cte(self):
        return self.customer_id


SESSION = ContextVar("demo_session", default=DemoSession())
REQUEST = ContextVar("demo_request", default="")
AUDIT = ContextVar("demo_audit", default=None)
OWN_ACCOUNT_REFS = ContextVar("own_account_refs", default=None)
CLIENT_CATALOG_QUALITY_MESSAGE = (
    "Con tu perfil Cliente puedes consultar tus propios saldos y movimientos. "
    "La revisión de calidad del catálogo general está reservada al perfil Empleado"
)
CLIENT_TECHNICAL_CATALOG_MESSAGE = "El catálogo técnico está reservado al perfil Empleado"


def requested_accounts(text):
    return frozenset(int(value) for value in re.findall(
        r"\b(?:cuentas?|num_cta)\s*(?:numero\s*)?[=:# ]*(\d+)\b", normalize(text)))


def foreign_client_alias_requested(question, session):
    if session is None or session.profile != "Cliente":
        return False
    aliases = set(re.findall(r"\bcliente_[a-z0-9]+(?:_[a-z0-9]+)*\b", normalize(question)))
    return bool(aliases - {normalize(session.principal or "")})


def technical_catalog_requested(question):
    return bool(requested_definition_fields(question) or re.search(
        r"\b(?:catalogo tecnico|diccionario de (?:variables|datos|tarjetas)|ddl|esquema sql)\b", normalize(question)))


def catalog_quality_requested(question):
    text = normalize(question)
    action = re.search(r"\b(?:cuant[oa]s?|revis\w*|comprob\w*|verific\w*|evalu\w*|detect\w*|cuent[ae])\b", text)
    control = re.search(r"\b(?:calidad|completitud|incomplet\w*|nulos?|duplicados?|huerfanos?|atipicos?|"
                        r"inconsistencias?|cobertura)\b", text)
    catalog = re.search(r"\b(?:catalogo|clientes|registros|datos globales)\b", text)
    own = re.search(r"\b(?:mi|mis|propios?|propias?)\b", text)
    general = re.search(r"\b(?:catalogo general|datos globales)\b", text)
    return bool(action and control and catalog and (general or not own))


def customer_completeness_rule(question):
    match = re.fullmatch(
        r"[¿¡\s]*(?:cuantos clientes (?:tienen|presentan|hay con)|cuenta los clientes con)"
        r"\s+(?:informacion incompleta|datos incompletos|registros incompletos)"
        r"(?:\s+(?:en (?:sus|los) registros|del catalogo general))?"
        r"(?P<detalle>\s+y cuales son los campos con mas datos faltantes)?[?!. ]*",
        normalize(question),
    )
    if match:
        return CUSTOMER_COMPLETENESS_DETAIL if match["detalle"] else "campos_basicos"
    return None


def load_session(profile="Analista"):
    if profile == "Analista":
        return DemoSession()
    config = json.loads((ROOT / "config/demo_sessions.json").read_text(encoding="utf-8"))
    return DemoSession("Cliente", config["client"]["num_cte"], config["client"]["num_cta"])


def employee_individual_request(question, *, after_ranking=False):
    text = normalize(question)
    if re.search(r"\b(?:cliente|titular|cuenta|num_cte|num_cta)\s*"
                 r"(?:(?:con\s+)?(?:numero|id|identificador)\s*)?[=:# ]*\d+\b"
                 r"|\b(?:id|identificador|numero)\s+(?:del?|de la)\s+(?:cliente|titular|cuenta)\s*[=:# ]*\d+\b"
                 r"|\b(?:cliente|titular)\s+(?:llamad[oa]|de nombre)\b"
                 r"|\b(?:datos|ficha|detalle|perfil)\s+(?:de un|del)\s+(?:cliente|titular)\s+especific[oa]\b", text):
        return True
    return after_ranking and bool(re.search(
        r"\b(?:sus (?:datos|cuentas|saldos)|su (?:nombre|cuenta|saldo|ficha|perfil)|ese cliente|esa persona|el de arriba"
        r"|(?:primer|segundo|tercer|cuarto|quinto)\s+(?:lugar|puesto|titular)"
        r"|(?:posicion|puesto)\s+[1-5]|titular\s+[a-e])\b", text))


def employee_ranking_requested(question):
    text = normalize(question)
    return bool(re.search(r"\b(?:clientes|titulares)\b", text) and
                re.search(r"\b(?:ranking|top|mayores saldos|mayor saldo)\b", text))


def mask_employee_ranking(balances):
    """Genera alias por posición, independientes de los identificadores reales."""
    return [{"posicion": index, "alias": f"Titular {letter}", "saldo": balance}
            for index, (letter, balance) in enumerate(zip("ABCDE", balances), 1)]


def request_denial():
    session = SESSION.get()
    if session is None:
        return "missing_identity"
    if session.profile == "Analista":
        return "employee_individual" if employee_individual_request(REQUEST.get()) else None
    text = normalize(REQUEST.get())
    if foreign_client_alias_requested(text, session):
        return "foreign_customer"
    if re.search(r"\b(otro|otra|ajeno|ajena)\b", text) and re.search(r"\b(cuenta|titular|saldo)\b", text):
        return "foreign_account"
    if type(session.num_cte) is not int or session.num_cte <= 0:
        return "missing_identity"
    verified = OWN_ACCOUNT_REFS.get()
    accounts = verified[1] if verified and verified[0] == session else (session.account_id,)
    if requested_accounts(text) - set(accounts):
        return "foreign_account"
    for value in re.findall(r"\b(?:cliente|num_cte)\s*[=:]?\s*(\d+)\b", text):
        if int(value) != session.customer_id:
            return "foreign_customer"
    if re.search(r"\b(consultar_datos_global|datos globales|saldo global|cuentas del banco)\b", text):
        return "aggregate_not_allowed"
    if re.search(r"\b(total|suman|agregado|todas las cuentas)\b", text) and not re.search(r"\b(mi|mis|propias|propios)\b", text):
        return "aggregate_not_allowed"
    return None


def authorize(operation):
    session = SESSION.get()
    if session is None:
        return {"status": "denied", "error_code": request_denial(),
                "message": "Acceso bloqueado. Inicia sesión para consultar datos."}
    reason = request_denial()
    allowed = reason is None
    if operation in {"aggregate_balance", "quality", "employee_ranking"} and session.profile != "Analista":
        allowed, reason = False, "analyst_only"
    if operation == "technical_catalog" and session.profile != "Analista":
        allowed, reason = False, "technical_catalog_employee_only"
    if operation == "own_analysis" and session.profile != "Cliente":
        allowed, reason = False, "client_only"
    if operation == "own_balance":
        text = normalize(REQUEST.get())
        own_request = bool(re.search(r"\b(mi|mis)\b", text) or re.search(r"\bcuenta\s+\d+\b", text))
        if session.profile != "Cliente" or not own_request:
            allowed, reason = False, "own_balance_requires_client_session"
    audit = AUDIT.get()
    if audit:
        audit.records.append(dict(event="authorization", agent="policy_code", operation=operation,
                                  profile=session.profile, allowed=allowed, reason=reason,
                                  sql_executed=False if not allowed else None))
    if not allowed:
        if reason == "technical_catalog_employee_only":
            return dict(status="denied", error_code=reason, profile=session.profile,
                        message=CLIENT_TECHNICAL_CATALOG_MESSAGE)
        if reason == "employee_individual":
            return dict(status="denied", error_code=reason, profile=session.profile,
                        message="El perfil Empleado no puede consultar el detalle de un cliente específico.")
        return dict(status="denied", error_code=reason, profile=session.profile,
                    message="Acceso bloqueado. Cliente solo puede consultar sus propias cuentas; la pregunta no modifica el perfil ni la identidad.")
    return None


def authorize_technical_catalog():
    denial = authorize("technical_catalog")
    if denial and denial["error_code"] == "technical_catalog_employee_only":
        audit = AUDIT.get()
        if audit:
            event = {"agent": "policy_code", "profile": denial["profile"], "reason": denial["error_code"]}
            audit.records.append({**event, "event": "access_blocked", "sql_executed": False})
            audit.records.append({**event, "event": "governance_result", "status": "denied"})
    return denial


def review_policy():
    session = SESSION.get()
    reason = request_denial()
    if session is None:
        return {"status": "denied", "profile": "Sin sesión", "reason": reason,
                "message": "Inicia sesión para consultar datos."}
    return dict(status="denied" if reason else "ok", profile=session.profile, reason=reason,
                rule="Cliente: solo cuentas propias, filtro obligatorio num_cte de sesión, sin agregados globales ni Quality. Analista: agregados y calidad, sin consulta personal de Cliente.",
                identity_source="contexto autenticado del servidor",
                policy_kind="simulated_demo_rules_not_banamex_policy")


def format_governance(result):
    prefix = "Acceso bloqueado. " if result["status"] == "denied" else ""
    return prefix + f"Perfil de sesión: {result['profile']}. " + result.get("rule", result.get("message", "")) + " La pregunta no eleva permisos. Reglas simuladas de la demo."
