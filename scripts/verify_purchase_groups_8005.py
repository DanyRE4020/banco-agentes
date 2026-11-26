"""Verifica agrupaciones y barras de compras propias con el chat real de 8005."""
from decimal import Decimal
from getpass import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

COPY = Path(__file__).resolve().parents[1]
PROJECT = COPY.with_name("banco-agentes")
REPORT = COPY / "verification-purchase-groups-8005.json"
CASES = (
    ("mes", "Desglosa en una tabla el importe de mis compras con tarjeta de crédito por mes, de febrero a junio de 2026."),
    ("trimestre", "Ahora muéstrame el total de mis compras por trimestre"),
    ("anio", "Dame una sola cifra acumulada por año para ese mismo intervalo."),
    ("barras", "En barras, agrupa esos gastos en bloques de tres meses."),
)


def check(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def evidence(profile):
    from banco_agentes.client_access import connect_client
    from banco_agentes.governance import SESSION

    token = SESSION.set(profile)
    try:
        with connect_client() as db:
            status = db.execute("SELECT current_user, current_database(), current_setting('transaction_read_only')").fetchone()
            check(tuple(status) == ("banco_cliente", "banco", "on"), "La conexión de control no confirmó banco_cliente.")
            result = db.execute("""
                SELECT cve_mes_txn, sum(saldo_txn)
                FROM cliente.movimientos
                WHERE cve_producto = %s AND cve_txn = %s AND cve_mes_txn BETWEEN %s AND %s
                GROUP BY cve_mes_txn ORDER BY cve_mes_txn
            """, ("TDC", 400, 202602, 202606)).fetchall()
    finally:
        SESSION.reset(token)
    check([month for month, _ in result] == list(range(202602, 202607)), "El rango de control no cubre cinco meses.")
    months = [amount for _, amount in result]
    return {"mes": months, "trimestre": [sum(months[:2]), sum(months[2:])], "anio": [sum(months)]}


def http_post(client, path, **kwargs):
    result = client.post(path, json=kwargs)
    if path == "/api/chat" and result.status_code in (503, 504):
        result = client.post(path, json=kwargs)
    check(result.status_code == 200, f"{path} devolvió HTTP {result.status_code}.")
    return result.json()


def verify_turn(result, grouping, expected, chart_requested, report):
    trace_id = result["trace_id"]
    audit = (COPY / "verification-web.jsonl").read_text(encoding="utf-8").splitlines()
    turn = next(row for row in map(json.loads, reversed(audit)) if row["trace_id"] == trace_id)
    ids = result["presentation"]["result_ids"]
    plans = [event for event in turn["trace"] if event.get("event") == "validated_plan"
             and event.get("agent") == "data_analytics"]
    report.update(trace_id=trace_id, respuesta=result["answer"], grupos=[e.get("plan", {}).get("agrupar") for e in plans],
                  herramientas=result["tools"])
    check(not result["blocked"] and len(ids) == 1 and "mis_gastos_por_mes" in result["tools"],
          "El chat no seleccionó un único resultado autorizado de compras propias.")
    chosen = [event for event in plans if event.get("id") == ids[0] and event.get("status") == "ok"]
    check(len(chosen) == 1, "Falta el plan verificado que eligió el chat.")
    event = chosen[0]
    plan = event["plan"]
    check(event.get("sql_executed") and plan["agrupar"] == [grouping]
          and (plan["desde"], plan["hasta"], plan["alcance"], plan["producto"], plan["codigo"]) ==
          ("2026-02", "2026-06", "propio", "TDC", 400),
          "Analytics cambió el alcance, el período o la agrupación.")
    check([Decimal(row["valor"]) for row in event["rows"]] == expected,
          "Los importes de Analytics difieren del SELECT de control.")
    parse_amount = lambda value: Decimal(value.replace(".", "").replace(",", "."))
    if chart_requested:
        charts = result["charts"]
        check(len(charts) == 1 and "preparar_grafica" in result["tools"]
              and charts[0]["type"] == "barras" and charts[0]["schema_version"] == 2
              and charts[0]["result_ids"] == ids and charts[0].get("purchase_group") == grouping,
              "Graph no devolvió barras Plotly para la agrupación seleccionada.")
        chart = charts[0]
        check(len(chart["points"]) == len(expected) and len(chart["series"]) == 1 and
              [Decimal(point["y"]) for point in chart["points"]] ==
              [Decimal(value) for value in chart["series"][0]["y"]] == expected,
              "Las barras no coinciden con los totales devueltos por Analytics.")
        check("parcial" in chart["points"][0]["label"] and "febrero a marzo" in chart["points"][0]["label"],
              "La cobertura de T1 quedó oculta en la gráfica.")
        report["barras"] = chart["points"]
    else:
        rows = [row for table in result["tables"] for row in table["rows"]]
        check(len(rows) == len(expected) and [parse_amount(row["valor"]) for row in rows] == expected,
              "La tabla no muestra las filas verificadas.")
        if grouping == "trimestre":
            check([row["trimestre"] for row in rows] ==
                  ["T1 2026 (parcial, febrero a marzo)", "T2 2026 (abril a junio)"], "La cobertura trimestral es incorrecta.")
        if grouping == "anio":
            check(rows[0]["anio"] == "2026 (parcial, febrero a junio)", "El año debe mostrarse como parcial.")
        report["filas"] = rows
    report.update(ok=True, agrupacion=grouping, periodo=[plan["desde"], plan["hasta"]])


def serve(logs):
    sys.path.insert(0, str(COPY))
    from banco_agentes.web import main

    sys.argv = ["banco_agentes.web", "--provider", "gemini", "--port", "8005",
                "--metrics-log", str(logs / "turn-metrics.jsonl")]
    main()


def main():
    sys.path.insert(0, str(PROJECT))
    from banco_agentes.auth import authenticate, read_users, users_path

    report = {"estado": "iniciando", "puerto": 8005, "casos": {}}
    child = None
    logs = Path(tempfile.mkdtemp(prefix="banco-compras-agrupacion-"))
    report["bitacoras"] = str(logs)

    def save(stage):
        report["etapa"] = stage
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(stage, flush=True)

    try:
        save("credenciales de la terminal y registro web real")
        check(listening(8004) and not listening(8005), "8004 no responde o 8005 está ocupado.")
        check(bool(os.getenv("BANCO_CLIENT_DB_PASSWORD")), "Falta BANCO_CLIENT_DB_PASSWORD en esta terminal.")
        check(bool(os.getenv("GOOGLE_API_KEY")), "Falta GOOGLE_API_KEY en esta terminal.")
        registry = users_path().resolve()
        password = (os.getenv("BANCO_CLIENTE_PASSWORD") if os.getenv("BANCO_CLIENTE_USERNAME", "cliente_a") == "cliente_a"
                    else None) or getpass("Contraseña web de cliente_a (entrada oculta): ")
        identity = authenticate("cliente_a", password, read_users(registry))
        check(identity and identity.profile == "Cliente", "El registro web no aceptó la sesión de cliente_a.")
        save("SELECT de control sobre las vistas de Cliente")
        expected = evidence(identity)
        check(expected["trimestre"] == [Decimal("2089.09"), Decimal("3321.93")]
              and expected["anio"] == [Decimal("5411.02")], "El volumen no produjo los totales esperados.")
        report["control_sql"] = {name: [str(v) for v in values] for name, values in expected.items()}
        env = os.environ.copy()
        env.pop("BANCO_CLIENTE_PASSWORD", None)
        env.update(BANCO_USERS_FILE=str(registry), PYTHONIOENCODING="utf-8")
        save("arranque de la copia en 8005")
        with (logs / "stdout.log").open("wb") as stdout, (logs / "stderr.log").open("wb") as stderr:
            child = subprocess.Popen([sys.executable, "-B", __file__, "--serve", str(logs)], cwd=COPY, env=env,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        with httpx.Client(base_url="http://127.0.0.1:8005", timeout=430, trust_env=False) as client:
            for _ in range(120):
                check(child.poll() is None, "8005 terminó durante el arranque.")
                try:
                    if client.get("/", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.4)
            else:
                raise RuntimeError("8005 no respondió al arrancar.")
            check(http_post(client, "/api/login", username="cliente_a", password=password)["profile"] == "Cliente",
                  "El chat de prueba no confirmó el perfil Cliente.")
            cid = http_post(client, "/api/conversations")["conversation_id"]
            for key, question in CASES:
                save(key)
                case = report["casos"][key] = {"pregunta": question}
                result = http_post(client, "/api/chat", question=question, conversation_id=cid)
                group = "trimestre" if key == "barras" else key
                verify_turn(result, group, expected[group], key == "barras", case)
                save(key)
            http_post(client, "/api/logout")
        report["estado"] = "verificado"
        save("tres agrupaciones y barras trimestrales verificadas en 8005")
    except (Exception, KeyboardInterrupt) as error:
        report.update(estado="bloqueado", error=str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        save(report["etapa"])
    finally:
        if child is not None and child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return int(report["estado"] != "verificado")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]))
    else:
        sys.exit(main())
