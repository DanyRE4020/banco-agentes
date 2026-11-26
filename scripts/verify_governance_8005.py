"""Verificación HTTP aislada en 8005 con PostgreSQL de solo lectura."""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from getpass import getpass
from pathlib import Path

import httpx

ORIGINAL = Path("C:/Users/Didi/Documents/banco-agentes")
COPY = ORIGINAL.with_name("banco-agentes-governance-prueba-20261119")
PORT = 8005
APP_KEYS = {"host": "APP_DB_HOST", "port": "APP_DB_PORT", "dbname": "APP_DB_NAME",
            "user": "APP_DB_USER", "password": "APP_DB_PASSWORD"}
RANKING = "Muestra el top 5 de clientes por saldo de AHORRO al cierre de junio de 2026."


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def reference(client_only):
    from banco_agentes.analytics import connect_readonly
    from banco_agentes.auth import read_users, users_path
    from banco_agentes.client_access import connect_client
    from banco_agentes.governance import DemoSession, SESSION

    if client_only:
        user = read_users(users_path()).get("cliente_a", {})
        require(user.get("rol") == "Cliente" and type(user.get("num_cte")) is int,
                "El registro web del proyecto no contiene la identidad esperada.")
        token = SESSION.set(DemoSession("Cliente", user["num_cte"], principal="cliente_a"))
        try:
            with connect_client() as db:
                role, database, readonly = db.execute(
                    "SELECT current_user, current_database(), current_setting('transaction_read_only')"
                ).fetchone()
                require((role, database, readonly) == ("banco_cliente", "banco", "on"),
                        "PostgreSQL no confirmó banco_cliente en solo lectura.")
                rows = db.execute("""
                    SELECT DISTINCT ON (num_cta, cve_producto) num_cta, cve_producto, saldo_mes
                    FROM cliente.saldos WHERE num_cte = %s
                    ORDER BY num_cta, cve_producto, fec_informacion DESC
                """, (user["num_cte"],)).fetchall()
            return {(str(account), product): Decimal(str(amount)).quantize(Decimal("0.01"))
                    for account, product, amount in rows}
        finally:
            SESSION.reset(token)

    with connect_readonly() as db:
        role, database, readonly = db.execute(
            "SELECT current_user, current_database(), current_setting('transaction_read_only')"
        ).fetchone()
        require((role, database, readonly) == ("banco_lectura", "banco", "on"),
                "PostgreSQL no confirmó banco_lectura en solo lectura.")
        rows = db.execute("""
            WITH saldos_por_persona AS (
                SELECT num_cte, round(sum(saldo_mes)::numeric, 2) AS importe
                FROM cfg.fact_sdos_captacoloca_m
                WHERE fec_informacion = %s AND cve_producto = %s
                GROUP BY num_cte
            )
            SELECT importe FROM saldos_por_persona
            WHERE importe IS NOT NULL
            ORDER BY importe DESC, num_cte ASC FETCH FIRST 5 ROWS ONLY
        """, (20260630, "AHORRO")).fetchall()
    return [amount for (amount,) in rows]


def money(value):
    return Decimal(value.replace(".", "").replace(",", "."))


def post(http, path, **data):
    result = http.post(path, json=data)
    require(result.status_code == 200, f"{path} devolvió HTTP {result.status_code}.")
    return result.json()


def prove_employee(http, password, expected, audit_path, report):
    require(post(http, "/api/login", username="empleado_prueba", password=password).get("profile") == "Analista",
            "El inicio de sesión no confirmó Empleado.")
    cid = post(http, "/api/conversations")["conversation_id"]
    result = post(http, "/api/chat", question=RANKING, conversation_id=cid)
    require(not result["blocked"] and result["agents"] == ["data_steward", "data_analytics"],
            "Steward y Analytics no completaron el ranking.")
    rows = result["tables"][0]["rows"]
    require(len(rows) == 5 and [row["alias"] for row in rows] == [f"Titular {c}" for c in "ABCDE"]
            and [row["posicion"] for row in rows] == list(range(1, 6))
            and all(set(row) == {"alias", "posicion", "saldo"} for row in rows),
            "El ranking no está enmascarado o cambió el orden de los puestos.")
    require([money(row["saldo"]) for row in rows] == expected, "Los saldos no coinciden con PostgreSQL.")
    report["ranking"] = {"ok": True, "filas": 5, "sql_de_control": True, "trace_id": result.get("trace_id"),
                         "roles": result["agents"], "tabla": rows}
    denied = []
    for question in ("¿Cuál es el saldo del cliente número 2?",
                     "¿Puedes mostrar la ficha del primer puesto del ranking?"):
        result = post(http, "/api/chat", question=question, conversation_id=cid)
        require(result["blocked"] and not result["tables"] and not result["charts"]
                and result["agents"] == ["data_steward"], "La petición individual no quedó bloqueada.")
        require(result["sources"] and "DG-01" in result["sources"][0]["location"]
                and "DG-01" in result["answer"], "Steward no citó la regla del bloqueo.")
        trace_id = result.get("trace_id")
        entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        entry = next(row for row in reversed(entries) if row["trace_id"] == trace_id)
        require(any(event.get("event") == "access_blocked" and event.get("sql_executed") is False
                    for event in entry["trace"]) and not any(event.get("sql_executed") for event in entry["trace"]),
                "La bitácora no confirmó el bloqueo antes de SQL.")
        denied.append({"caso": "por número" if not denied else "por puesto",
                       "sql_executed": False, "trace_id": trace_id, "cita": result["sources"][0]})
    report["bloqueo"] = {"ok": True, "casos": denied}


def prove_client(http, password, expected, report):
    require(post(http, "/api/login", username="cliente_a", password=password).get("profile") == "Cliente",
            "El inicio de sesión no confirmó cliente_a.")
    cid = post(http, "/api/conversations")["conversation_id"]
    result = post(http, "/api/chat", question="¿Cuáles son mis cuentas?", conversation_id=cid)
    require(not result.get("blocked") and "mis_cuentas" in result["tools"],
            "El chat no confirmó mis_cuentas.")
    rows = [row for table in result["tables"] for row in table["rows"] if "cuenta" in row]
    require(len(rows) == len(expected) == 4 and {(row["cuenta"], row["producto"]) for row in rows} == set(expected),
            "Las cuatro cuentas no coinciden con la vista filtrada de PostgreSQL.")
    for row in rows:
        product = row["producto"]
        balance = expected[(row["cuenta"], product)]
        net = -balance if product in {"TDC", "C_PERSONAL"} else balance
        field, empty = ("saldo_a_favor", "deuda") if net >= 0 else ("deuda", "saldo_a_favor")
        require(money(row[field]) == abs(net) and row[empty] == "Sin valor",
                "No coincide la separación de saldo a favor y deuda.")
    report["cliente_a"] = {"ok": True, "cuentas": 4, "coincide_con_vista": True, "trace_id": result.get("trace_id"),
                           "productos": sorted(row["producto"] for row in rows)}


def serve(location):
    sys.path.insert(0, str(COPY))
    from banco_agentes.web import main

    sys.argv = ["banco_agentes.web", "--provider", "gemini", "--port", str(PORT),
                "--metrics-log", str(location / "8005.metrics.jsonl")]
    main()


def main(client_only):
    sys.path.insert(0, str(ORIGINAL))
    from banco_agentes.analytics import connection_settings
    from banco_agentes.auth import password_record, read_users, users_path

    label = "cliente" if client_only else "empleado"
    report = {"estado": "iniciando", "prueba": label, "puerto": PORT}
    report_path = COPY / f"verification-governance-{label}-8005.json"

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    with tempfile.TemporaryDirectory(prefix="banco-governance-8005-") as folder:
        location = Path(folder)
        process = None
        try:
            require(listening(8004) and not listening(PORT), "8004 no está disponible o 8005 ya está ocupado.")
            env = os.environ.copy()
            for key in APP_KEYS.values():
                os.environ.pop(key, None)
            settings = connection_settings()
            require(tuple(settings[k] for k in ("host", "port", "dbname", "user"))
                    == ("127.0.0.1", 55432, "banco", "banco_lectura"),
                    "La configuración local no apunta a banco y banco_lectura.")
            env.update({APP_KEYS[key]: str(settings[key]) for key in APP_KEYS})
            env["PYTHONIOENCODING"] = "utf-8"
            if client_only:
                require(bool(env.get("BANCO_CLIENT_DB_PASSWORD")),
                        "Falta BANCO_CLIENT_DB_PASSWORD en esta terminal.")
                registry = users_path().resolve()
                require("cliente_a" in read_users(registry), "El registro web no incluye cliente_a.")
                password = (env.get("BANCO_CLIENTE_PASSWORD") if env.get("BANCO_CLIENTE_USERNAME", "cliente_a") == "cliente_a"
                            else None) or getpass("Contraseña web de cliente_a (entrada oculta): ")
                require(bool(password), "La contraseña web de cliente_a es obligatoria.")
                report["registro_web"] = "proyecto original"
                report["rol_postgresql"] = "banco_cliente"
            else:
                registry = location / "users.json"
                password = secrets.token_urlsafe(32)
                registry.write_text(json.dumps({"empleado_prueba": {"rol": "Empleado", "num_cte": None,
                                                                    **password_record(password)}}), encoding="utf-8")
                report["registro_web"] = "Empleado temporal de prueba"
                report["rol_postgresql"] = "banco_lectura"
            env["BANCO_USERS_FILE"] = str(registry)
            env.pop("BANCO_CLIENTE_PASSWORD", None)
            report["etapa"] = "SQL de control de solo lectura"
            save()
            expected = reference(client_only)
            with (location / "stdout.log").open("wb") as stdout, (location / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen([sys.executable, "-B", __file__, "--serve", folder], cwd=COPY,
                                           env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                           creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            report["etapa"] = "arranque en 8005"
            save()
            with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=430, trust_env=False) as http:
                for _ in range(100):
                    require(process.poll() is None, "La instancia terminó durante el arranque.")
                    try:
                        if http.get("/", timeout=1).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.4)
                else:
                    raise RuntimeError("8005 no respondió en 40 segundos.")
                report["etapa"] = "chat autenticado en 8005"
                save()
                if client_only:
                    prove_client(http, password, expected, report)
                else:
                    prove_employee(http, password, expected, COPY / "verification-web.jsonl", report)
            report["estado"] = "verificado"
        except RuntimeError as error:
            report.update(estado="bloqueado", error=str(error))
        except (Exception, KeyboardInterrupt) as error:
            report.update(estado="bloqueado", error=type(error).__name__)
        finally:
            if process is not None and process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            save()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return int(report["estado"] != "verificado")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]))
    else:
        sys.exit(main(sys.argv[1:] == ["--cliente"]))
