import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.conversation import Conversation
from banco_agentes.governance import DemoSession
from banco_agentes.plans import execute_plan
from web_fixtures import create_app


class PlannedModel(BaseLlm):
    agent: str

    async def generate_content_async(self, llm_request, stream=False):
        content = llm_request.contents[-1]
        response = next((part.function_response for part in content.parts if part.function_response), None)
        graph_followup = any("Grafícalo" in (p.text or "") for c in llm_request.contents for p in c.parts or [])
        if self.agent == "orquestador" and response and response.name == "data_analytics" and graph_followup:
            ids = [r["id"] for r in response.response["evidence"]]
            part = self.call("data_graph", request="Grafica como linea " + ", ".join(ids))
        elif response is not None:
            part = types.Part(text="Revisé el resultado de la herramienta.")
        else:
            question = "".join(p.text or "" for p in content.parts)
            if self.agent == "orquestador":
                instruction = str(llm_request.config.system_instruction)
                if question == "Solo PF":
                    assert '"id": "R1"' in instruction and "CHEQUES" in instruction
                    brief = "Total de saldos CHEQUES marzo con persona PF"
                elif question == "Compara con febrero":
                    assert '"persona": "PF"' in instruction
                    brief = "Variación de saldos CHEQUES PF febrero y marzo"
                elif question == "Grafícalo":
                    assert '"ultimos_resultados": ["R3"]' in instruction
                    brief = "Variación de saldos CHEQUES PF febrero y marzo; consulta valores nuevos"
                else:
                    brief = "Total de saldos CHEQUES marzo"
                part = self.call("data_analytics", request=brief)
            elif self.agent == "data_graph":
                import re
                ids = list(dict.fromkeys(re.findall(r"\bR[1-9][0-9]*\b", question)))
                part = self.call("preparar_grafica", tipo="linea", resultados=ids)
            elif "Variación" in question:
                part = self.call("comparar_mes_anterior", resultado="R2")
            else:
                args = dict(metrica="total", producto="CHEQUES", periodo="2026-03")
                if "PF" in question:
                    args["opciones"] = "persona=PF"
                part = self.call("consultar_datos", **args)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))

    @staticmethod
    def call(name, **args):
        return types.Part(function_call=types.FunctionCall(name=name, args=args))


class DependencyModel(BaseLlm):
    agent: str

    async def generate_content_async(self, llm_request, stream=False):
        parts = llm_request.contents[-1].parts
        response = next((part.function_response for part in parts if part.function_response), None)
        if self.agent == "orquestador" and response is None:
            part = PlannedModel.call("data_steward", request="Obtén la clave esperada de movimientos.")
        elif self.agent == "orquestador" and response.name == "data_steward":
            evidence = response.response["evidence"][0]
            brief = "Comprueba duplicados en febrero por " + ", ".join(evidence["expected_key"])
            part = PlannedModel.call("data_quality", request=brief)
        elif response is not None:
            part = types.Part(text="Revisión de evidencia completada.")
        elif self.agent == "data_steward":
            part = PlannedModel.call("consultar_reglas", conjunto="movimientos")
        else:
            assert "num_cta, cve_producto, cve_mes_txn, cve_txn" in parts[0].text
            part = PlannedModel.call("revisar_calidad", metrica="duplicados", conjunto="movimientos", periodo="2026-02")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def simulated_plan(plan, *, quality=False):
    plan = {**plan, "hasta": plan["hasta"] or plan["desde"], "agrupar": ()}
    rows = [dict(valor="20", filas=2, observados=2)]
    if plan["metrica"] == "variacion":
        plan["agrupar"] = ("mes",)
        rows = [dict(mes=202602, valor="15", filas=2, observados=2),
                dict(mes=202603, valor="20", filas=2, observados=2)]
    return dict(status="ok", plan=plan, rows=rows, columns=list(rows[0]), sources=["referencias/datos.sql:1"],
                table="cfg.fact_sdos_captacoloca_m", notes=[], rule=None, sql_executed=True, units="Importe sintético")


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_adk_session_passes_filters_and_refreshes_values_before_graph(self):
        conversation = Conversation(DemoSession(), model_factory=lambda name: PlannedModel(model="test", agent=name))
        questions = ["Total en marzo", "Solo PF", "Compara con febrero", "Grafícalo"]
        with patch("banco_agentes.general_agents.execute_plan", side_effect=simulated_plan) as sql:
            turns = [await conversation.ask(question) for question in questions]
        sessions = [turn["trace"][0] for turn in turns]
        self.assertEqual(len({row["session_id"] for row in sessions}), 1)
        self.assertEqual([row["reused"] for row in sessions], [False, True, True, True])
        self.assertEqual(sql.call_count, 4)
        for turn in turns[:2]:
            self.assertFalse(any(event.get("tool") == "data_governance" for event in turn["trace"]))
            self.assertNotIn("Perfil de sesión:", turn["answer"])
        self.assertEqual(sql.call_args_list[1].args[0]["persona"], "PF")
        self.assertEqual(sql.call_args_list[2].args[0]["persona"], "PF")
        self.assertEqual(turns[-1]["charts"][0]["type"], "linea")
        self.assertEqual([p["y"] for p in turns[-1]["charts"][0]["points"]], ["15", "20"])
        self.assertTrue(any(row["event"] == "chart_prepared" for row in turns[-1]["trace"]))
        tools = [e["tool"] for e in turns[-1]["trace"] if e["event"] == "tool_started"]
        self.assertEqual(tools, ["data_analytics", "comparar_mes_anterior", "data_graph", "preparar_grafica"])
        self.assertEqual(turns[-1]["charts"][0]["result_ids"], ["R4"])

    async def test_hub_relays_steward_evidence_to_quality(self):
        conversation = Conversation(DemoSession(), model_factory=lambda name: DependencyModel(model="test", agent=name))
        with patch("banco_agentes.general_agents.execute_plan", side_effect=simulated_plan) as execute:
            result = await conversation.ask("Confirma la clave de movimientos y revisa duplicados en febrero.")
        assert execute.call_args.kwargs["quality"] is True
        self.assertEqual(execute.call_args.args[0]["conjunto"], "movimientos")
        started = [event["tool"] for event in result["trace"] if event["event"] == "tool_started"]
        self.assertEqual(started, ["data_steward", "consultar_reglas", "data_quality", "revisar_calidad"])

    def test_plan_rejects_unknown_field_before_opening_database(self):
        plan = dict(metrica="total", conjunto="saldos", desde="2026-03", hasta="", producto="",
                    persona="", segmento="", agrupar="", alcance="global", codigo=0,
                    campo="un_campo_fuera_del_catalogo", minimo=None, maximo=None)
        with patch("banco_agentes.plans.connect_readonly") as db:
            result = execute_plan(plan)
        self.assertEqual(result["status"], "invalid_plan")
        db.assert_not_called()


class ConversationWebTests(unittest.TestCase):
    def test_conversation_id_cannot_cross_login_sessions(self):
        async def reply(question, *, session, timeout):
            return {"answer": question}

        app = create_app(runner=reply, audit_path=None)
        first = TestClient(app, base_url="http://localhost")
        second = TestClient(app, base_url="http://localhost")
        for client in (first, second):
            login = client.post("/api/login", json={"username":"analista", "password":"AnalistaDemo!2026"})
            self.assertEqual(login.status_code, 200)
        chat = first.post("/api/conversations", json={}).json()["conversation_id"]
        payload = {"question":"Hola", "conversation_id":chat}
        self.assertEqual(second.post("/api/chat", json=payload).status_code, 404)
        self.assertEqual(first.post("/api/chat", json=payload).status_code, 200)
        self.assertEqual(first.post("/api/logout", json={}).status_code, 200)
        self.assertEqual(first.post("/api/chat", json=payload).status_code, 401)


class PolicyModel(PlannedModel):
    async def generate_content_async(self, llm_request, stream=False):
        parts=llm_request.contents[-1].parts
        question="".join(p.text or "" for p in parts)
        if any(p.function_response for p in parts):
            part=types.Part(text="Resultado revisado.")
        elif self.agent == "data_governance":
            assert "permisos" in question.lower() or "bloqueado" in question.lower()
            part=self.call("explicar_politica_sesion")
        elif self.agent == "orquestador" and ("permisos" in question.lower() or "bloqueado" in question.lower()):
            part=self.call("data_governance",request=question)
        else:
            async for response in super().generate_content_async(llm_request,stream=stream):
                yield response
            return
        yield LlmResponse(content=types.Content(role="model",parts=[part]))


class PolicyExplanationTests(unittest.IsolatedAsyncioTestCase):
    def conversation(self, identity):
        return Conversation(identity,model_factory=lambda name: PolicyModel(model="test",agent=name))

    async def test_explicit_permissions_delegate_without_querying_data(self):
        conversation=self.conversation(DemoSession())
        with patch("banco_agentes.plans.connect_readonly") as db:
            result=await conversation.ask("¿Qué permisos tiene mi perfil?")
        db.assert_not_called()
        tools=[e.get("tool") for e in result["trace"] if e["event"]=="tool_started"]
        self.assertEqual(tools,["data_governance","explicar_politica_sesion"])
        self.assertIn("Perfil de sesión: Analista",result["answer"])
        self.assertTrue(any(e["event"]=="delegation_request" and "permisos" in e["request"] for e in result["trace"]))

    async def test_authorized_own_query_needs_no_explanation_but_keeps_server_filter(self):
        from decimal import Decimal
        from types import SimpleNamespace
        conversation=self.conversation(DemoSession("Cliente",1,200000))
        db=MagicMock()
        readonly=MagicMock()
        readonly.fetchone.return_value=("on",)
        cursor=MagicMock()
        cursor.description=[SimpleNamespace(name=name) for name in ("valor","filas","observados")]
        cursor.fetchall.return_value=[(Decimal("783.28"),1,1)]
        db.execute.side_effect=[readonly,cursor]
        with patch("banco_agentes.plans.connect_client") as connect:
            connect.return_value.__enter__.return_value=db
            result=await conversation.ask("Mi saldo de CHEQUES en marzo")
        sql,params=db.execute.call_args.args
        self.assertIn("f.num_cte = %s",sql)
        self.assertIn(1,params)
        self.assertTrue(any(e["event"]=="authorization" and e["allowed"] for e in result["trace"]))
        self.assertIn("783.28",result["answer"])
        self.assertNotIn("Perfil de sesión:",result["answer"])
        self.assertFalse(any(e.get("tool")=="data_governance" for e in result["trace"]))
        session=await conversation.service.get_session(app_name="banco_agentes_web",user_id=conversation.owner,session_id=conversation.id)
        self.assertEqual(session.state["verified_memory"]["results"][0]["scope"],"propio")

    async def test_denial_still_explains_and_prevents_sql(self):
        conversation=self.conversation(DemoSession("Cliente",1,200000))
        with patch("banco_agentes.plans.connect_readonly") as db:
            result=await conversation.ask("Explica por qué está bloqueado el saldo de otra cuenta")
        db.assert_not_called()
        self.assertIn("Acceso bloqueado",result["answer"])
        self.assertTrue(any(e.get("tool")=="explicar_politica_sesion" for e in result["trace"]))
        self.assertFalse(any(e["event"]=="validated_plan" and e.get("sql_executed") for e in result["trace"]))
