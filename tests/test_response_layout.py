import json
import unittest
from unittest.mock import patch

from test_presentation import result, turn_with
from banco_agentes.conversation import render_turn,presentation_fields,Conversation
from banco_agentes.response_layout import decode_final,PresentationChoice
from banco_agentes.charts import chart_for_results
from banco_agentes.governance import REQUEST,SESSION,DemoSession
from test_general import PlannedModel,simulated_plan
from google.genai import types
from google.adk.models.llm_response import LlmResponse


class LayoutTests(unittest.TestCase):
    def render(self,values,question,choice=None,prepared=None):
        turn=turn_with(values[0])
        turn.results=values
        turn.presentation_choice=choice or PresentationChoice()
        if prepared:
            try:
                turn.charts = [chart_for_results(values, prepared)]
            except ValueError as error:
                turn.chart_issues.append(str(error))
        token=REQUEST.set(question)
        try:
            answer,charts=render_turn(turn,"Texto numérico no verificado: 999999999.")
            fields=presentation_fields(turn)
        finally:
            REQUEST.reset(token)
        self.assertNotIn("999999999",answer)
        return answer,charts,fields,turn

    def test_explicit_table_for_one_total_and_explicit_prose_for_many(self):
        text,charts,fields,_=self.render([result()],"Muéstralo en una tabla",PresentationChoice(formato="frase"))
        self.assertEqual(fields["presentation"]["format"],"tabla")
        self.assertEqual(fields["tables"][0]["rows"][0]["valor"],"51,676,391.38")
        self.assertEqual(charts,[])
        other=result(producto="AHORRO");other["id"]="R2";other["rows"][0]["valor"]="20.00"
        text,charts,fields,_=self.render([result(),other],"Compara en prosa, sin tabla",PresentationChoice(formato="tabla"))
        self.assertEqual(fields["presentation"]["format"],"prosa")
        self.assertEqual(fields["tables"],[])
        self.assertIn("mayor",text)

    def test_comparison_across_results_answers_winner_and_preserves_precision(self):
        a=result();b=result(producto="AHORRO");b["id"]="R2"
        a["rows"][0]["valor"]="12345678901234567.8901"
        b["rows"][0]["valor"]="12345678901234567.8900"
        text,_,fields,_=self.render([a,b],"¿Cuál fue mayor?")
        self.assertTrue(text.startswith("El saldo total mayor corresponde a CHEQUES"))
        self.assertIn("diferencia es 0.0001",text)
        self.assertEqual(len(fields["tables"][0]["rows"]),2)

    def test_monthly_winner_overrides_generic_trend_and_ties_are_explicit(self):
        a=result("total",desde="2026-02",agrupar=("mes",))
        a["rows"]=[dict(mes=202602,valor="30.00"),dict(mes=202603,valor="20.00")]
        a["columns"]=["mes","valor"]
        text,_,_,_=self.render([a],"¿En qué mes fue mayor?",PresentationChoice(objetivo="tendencia"))
        self.assertIn("mayor corresponde a febrero de 2026",text)
        a["rows"][1]["valor"]="30.00"
        text,_,_,_=self.render([a],"¿Cuál fue mayor?")
        self.assertIn("empate",text)

    def test_auto_quality_table_retains_controls_months_and_zeroes(self):
        values=[]
        for month in ("2026-02","2026-03"):
            for metric in ("completitud","duplicados"):
                a=result(metric,desde=month,hasta=month)
                a["agent"]="data_quality";a["id"]="R"+str(len(values)+1)
                a["rows"]=[dict(filas=5000,**({"nulos":0} if metric=="completitud" else {"grupos_duplicados":0,"filas_adicionales":0}))]
                a["columns"]=list(a["rows"][0]);values.append(a)
        text,_,fields,_=self.render(values,"Revisa ambos controles por mes")
        self.assertIn("4 comprobaciones",text)
        rows=fields["tables"][0]["rows"]
        self.assertEqual(len(rows),4)
        self.assertEqual([r["nulos"] for r in rows],["0","No aplica","0","No aplica"])

    def test_unlike_metrics_or_incomplete_data_do_not_produce_a_winner(self):
        a=result();b=result("promedio");b["id"]="R2"
        text,_,_,_=self.render([a,b],"Compara cuál fue mayor")
        self.assertIn("no permiten determinar",text)
        b=result();b["status"]="partial";b["notes"]=["Faltan observaciones."]
        text,_,_,_=self.render([a,b],"Cuál fue mayor")
        self.assertIn("no permiten determinar",text)
        self.assertIn("Faltan observaciones",text)

    def test_native_final_envelope_and_invalid_metadata_fall_back_safely(self):
        choice,answer=decode_final(json.dumps(dict(presentacion=dict(formato="prosa"),respuesta="Una definición.")))
        self.assertEqual(choice.formato,"prosa");self.assertEqual(answer,"Una definición.")
        choice,answer=decode_final(json.dumps(dict(presentacion=dict(formato="html",rows=[{"valor":"999"}]),respuesta="Una definición.")))
        self.assertEqual(choice.formato,"auto");self.assertNotIn("999",answer)
        choice,_=decode_final(json.dumps(dict(presentacion=dict(resultados=["../../.env"]),respuesta="")))
        self.assertEqual(choice.resultados,[])

    def test_graph_prepared_series_is_presented_without_sql_or_llm(self):
        a=result(desde="2026-02",agrupar=("mes",))
        a["rows"]=[dict(mes=202602,valor="10.00"),dict(mes=202603,valor="20.00")];a["columns"]=["mes","valor"]
        with patch("banco_agentes.plans.connect_readonly") as db:
            text,charts,fields,turn=self.render([a],"Muestra la tendencia",PresentationChoice(formato="grafica",objetivo="tendencia",tipo_grafica="linea"),prepared="linea")
        db.assert_not_called()
        self.assertEqual(charts[0]["type"],"linea")
        self.assertEqual([p["y"] for p in charts[0]["points"]],["10.00","20.00"])
        self.assertEqual(fields["tables"],[])
        self.assertFalse(any(e["event"]=="presentation_chart_prepared" for e in turn.trace.records))

    def test_balance_chart_describes_changes_and_names_requested_peak(self):
        cheques = result(desde="2026-01", hasta="2026-06", agrupar=("mes",))
        ahorro = result(producto="AHORRO", desde="2026-01", hasta="2026-06", agrupar=("mes",))
        ahorro["id"] = "R2"
        cheques["rows"] = [{"mes": 202601, "valor": "50160000"}, {"mes": 202604, "valor": "54000000"},
                          {"mes": 202606, "valor": "53240000"}]
        ahorro["rows"] = [{"mes": 202601, "valor": "24870000"}, {"mes": 202606, "valor": "26400000"}]
        for value in (cheques, ahorro):
            value["columns"] = ["mes", "valor"]
        choice = PresentationChoice(formato="grafica", objetivo="mayor", tipo_grafica="linea")
        evolution, _, _, _ = self.render([cheques, ahorro], "Grafica la evolución", choice, "linea")
        self.assertTrue(evolution.startswith("De enero a junio de 2026."))
        self.assertIn("- CHEQUES pasó de 50,160,000 a 53,240,000.", evolution)
        self.assertIn("- AHORRO pasó de 24,870,000 a 26,400,000.", evolution)
        peak, _, _, _ = self.render([cheques, ahorro], "¿En qué producto y mes fue mayor el saldo?", choice, "linea")
        self.assertIn("CHEQUES en abril de 2026, con 54,000,000", peak)
        table_peak, _, _, _ = self.render([cheques, ahorro], "¿En qué producto y mes fue mayor el saldo?")
        self.assertIn("abril de 2026 (CHEQUES), con 54,000,000", table_peak)
        ahorro["rows"][0]["mes"] = 202602
        partial, _, _, _ = self.render([cheques, ahorro], "Grafica la evolución", choice, "linea")
        self.assertIn("De febrero a junio de 2026, AHORRO pasó de 24,870,000 a 26,400,000.", partial)

    def test_explicit_chart_kind_overrides_automatic_type(self):
        a=result(desde="2026-02",agrupar=("mes",))
        a["rows"]=[dict(mes=202602,valor="10.00"),dict(mes=202603,valor="20.00")];a["columns"]=["mes","valor"]
        _,charts,_,_=self.render([a],"Muéstralo en una gráfica de barras",PresentationChoice(formato="grafica"),prepared="barras")
        self.assertEqual(charts[0]["type"],"barras")

    def test_unsupported_chart_falls_back_to_table_with_reason(self):
        text,charts,fields,_=self.render([result()],"Grafícalo",PresentationChoice(formato="grafica",tipo_grafica="linea"),prepared="linea")
        self.assertEqual(charts,[])
        self.assertEqual(fields["presentation"]["format"],"tabla")
        self.assertIn("línea requiere",text)

    def test_presentation_never_builds_a_chart_without_graph(self):
        text,charts,fields,turn=self.render([result()],"Grafícalo",PresentationChoice(formato="grafica"))
        self.assertEqual(charts,[])
        self.assertEqual(fields["presentation"]["format"],"tabla")
        self.assertIn("Graph no preparó",text)
        self.assertFalse(any(e["event"]=="presentation_chart_prepared" for e in turn.trace.records))

    def test_quality_counts_compare_by_month_without_mixing_controls(self):
        values=[]
        for month,n in (("2026-02",0),("2026-03",2)):
            a=result("duplicados",desde=month,hasta=month)
            a["agent"]="data_quality";a["id"]="R"+str(len(values)+1)
            a["rows"]=[dict(filas=5,grupos_duplicados=n,filas_adicionales=n)]
            a["columns"]=list(a["rows"][0]);values.append(a)
        text,_,_,_=self.render(values,"¿En qué mes hubo mayor número de duplicados?")
        self.assertIn("número de grupos duplicados mayor",text)
        self.assertIn("marzo de 2026",text)

    def test_memory_result_cannot_cross_the_server_profile(self):
        turn=turn_with(result())
        turn.memory={"results":turn.results};turn.results=[]
        turn.presentation_choice=PresentationChoice(formato="tabla",resultados=["R1"])
        a,b=SESSION.set(DemoSession("Cliente",1,200000)),REQUEST.set("Muéstralo en tabla")
        try:
            answer,charts=render_turn(turn,"No hay evidencia autorizada.")
            fields=presentation_fields(turn)
        finally:
            SESSION.reset(a);REQUEST.reset(b)
        self.assertNotIn("51,676,391",answer)
        self.assertEqual(fields["tables"],[])
        self.assertEqual(fields["sources"],[])

    def test_conceptual_prose_uses_the_current_hub_explanation_and_keeps_sources(self):
        turn=turn_with(result());turn.results=[]
        turn.documentation=["Definición recuperada del catálogo."]
        turn.sources=["referencias/definicion.sql:3"]
        turn.presentation_choice=PresentationChoice(formato="prosa")
        answer,_=render_turn(turn,"El cierre representa la existencia al final del mes.")
        fields=presentation_fields(turn)
        self.assertIn("existencia al final",answer)
        self.assertEqual(fields["presentation"]["format"],"prosa")
        self.assertTrue(fields["sources"])
        self.assertEqual(fields["tables"],[])

    def test_reference_to_unknown_memory_result_does_not_invent_values(self):
        text,_,fields,_=self.render([result()],"En tabla",PresentationChoice(formato="tabla",resultados=["R999"]))
        self.assertEqual(fields["presentation"]["result_ids"],["R1"])
        self.assertIn("51,676,391.38",text)


class EnvelopeModel(PlannedModel):
    async def generate_content_async(self,llm_request,stream=False):
        if self.agent=="orquestador" and any(p.function_response for p in llm_request.contents[-1].parts):
            yield LlmResponse(content=types.Content(role="model",parts=[types.Part(text=json.dumps(dict(
                presentacion=dict(formato="tabla",objetivo="resumen",resultados=["R1"]),respuesta="999999999")))]))
        else:
            async for response in super().generate_content_async(llm_request,stream=stream):
                yield response


class LayoutADKTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_hub_final_call_selects_layout_without_extra_model_call(self):
        conversation=Conversation(DemoSession(),model_factory=lambda name:EnvelopeModel(model="test",agent=name))
        calls=[]
        original=EnvelopeModel.generate_content_async
        async def counted(model,request,stream=False):
            calls.append(model.agent)
            async for response in original(model,request,stream=stream):
                yield response
        with patch.object(EnvelopeModel,"generate_content_async",counted), patch("banco_agentes.general_agents.execute_plan",side_effect=simulated_plan) as sql:
            value=await conversation.ask("Total de CHEQUES marzo")
        self.assertEqual(calls.count("orquestador"),2)
        self.assertEqual(calls.count("data_analytics"),2)
        self.assertEqual(len(calls),4)
        self.assertEqual(sql.call_count,1)
        self.assertEqual(value["presentation"]["format"],"tabla")
        self.assertEqual(len(value["tables"][0]["rows"]),1)
        self.assertNotIn("999999999",value["answer"])
        self.assertEqual(sum(e["event"]=="presentation_selected" for e in value["trace"]),1)
        self.assertEqual([e["tool"] for e in value["trace"] if e["event"]=="tool_started"],["data_analytics","consultar_datos"])
