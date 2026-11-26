"""Síntesis fiel de los resultados que el modelo decidió obtener."""

from .analytics import format_balance
from .quality import format_quality
from .knowledge import format_documents
from .governance import format_governance
from google.adk.tools.agent_tool import AgentTool


class ResultAgentTool(AgentTool):
    """AgentTool real que entrega campos tipados, no conteos ambiguos en prosa."""

    def __init__(self, agent, records):
        super().__init__(agent=agent)
        self._records = records

    async def run_async(self, *, args, tool_context):
        if self.name == "data_governance":
            # La decisión se obtiene de SESSION/REQUEST, nunca de una identidad
            # o petición financiera reenviada como instrucción al explicador.
            args = {"request": "Explica la política de la sesión actual y la decisión de acceso aplicada. Consulta explicar_politica_sesion; no se solicita consultar saldos ni ejecutar la petición financiera."}
            self._records.append(dict(event="delegation_scoped", agent=self.name, task="session_policy_explanation", customer_data_forwarded=False))
        await super().run_async(args=args, tool_context=tool_context)
        results = [r for r in self._records if r.get("event") in {"sql_result", "governance_result"} and r["agent"] == self.name]
        if not results:
            return {"status": "not_verified", "message": "El agente no ejecutó su consulta."}
        latest = results[-1]
        policy_ready = any(r.get("event") == "governance_result" for r in self._records)
        if latest.get("profile") == "Cliente" and latest.get("event") == "sql_result" and not policy_ready:
            # Requisito de presentación, nunca autorización delegada al modelo.
            # El hub decide cómo obtener la explicación; aquí no se invoca otro agente.
            return {"status": "needs_policy_explanation", "query_status": latest["status"],
                    "required_source": "data_governance", "repeat_sql": False,
                    "message": "La consulta ya fue autorizada o bloqueada por código. Falta obtener la explicación del perfil de data_governance; consulta ese AgentTool y después responde. No repitas la consulta SQL."}
        return {key: value for key, value in latest.items() if key not in {"event", "seconds", "db_user"}}


def sql_report(records):
    sections = []
    # Solo resultados reales invocados: no decide qué agentes faltan ni los llama.
    for record in records:
        if record.get("event") == "retrieval_result":
            sections.append(format_documents(record["result"]))
            continue
        if record.get("event") == "governance_result":
            sections.append(format_governance(record))
            continue
        if record.get("event") != "sql_result":
            continue
        if record["agent"] == "data_analytics":
            base_label = "Saldo propio" if record.get("operation") == "own_balance" else "Saldo total"
            label = base_label if record["status"] == "ok" else base_label + " sin verificar"
            sections.append(label + ":\n" + format_balance(record))
        elif record["agent"] == "data_quality":
            label = "Duplicidad" if record["status"] == "ok" else "Duplicidad sin verificar"
            sections.append(label + ":\n" + format_quality(record))
    return "\n\n".join(sections)
