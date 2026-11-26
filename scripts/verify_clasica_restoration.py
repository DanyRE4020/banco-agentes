"""Verificación de Clásica y regresión de Cliente, con reversión si falla 8004."""
import contextlib
from getpass import getpass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx

CUT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.gettempdir())
PREVIOUS_REPORT = TEMP / "banco-restore-compras-live.json"
PREVIOUS_HELPER = TEMP / "banco-restore-compras-live.py"
spec = importlib.util.spec_from_file_location("banco_live", PREVIOUS_HELPER)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
PREVIOUS_CUT = live.CUT
live.REPORT = CUT / "verification-clasica-live.json"

FIXTURES = {"identificacion": "ine-ficticia-clasica.pdf", "cfdi_nomina": "cfdi-nomina-ficticio-clasica.pdf"}
FIELDS = {
    "edad_anios": ("identificacion", {"fecha_nacimiento"}),
    "vigencia_identificacion": ("identificacion", {"fecha_vencimiento"}),
    "ingreso_mensual": ("cfdi_nomina", {"neto_a_pagar", "periodicidad", "moneda"}),
}
QUESTION = "¿Qué requisitos de la Tarjeta Clásica puedo verificar con estos documentos y cuáles quedan por verificar?"


def verify_documents(port, password):
    stage = f"{port} · evaluación documental de Clásica"
    live.step(stage)
    output = live.report["puertos"][str(port)]
    result = output["documentos"] = {"http": {}}
    with live.http(port) as client:
        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            result["http"][path] = response.status_code
            live.report_save()
            return live.response_json(response)

        profile = post("/api/login", json={"username": "cliente_a", "password": password})
        live.check(profile.get("profile") == "Cliente", "El login documental no seleccionó el perfil Cliente.")
        conversation = post("/api/conversations")["conversation_id"]
        files = [("files", (name, (CUT / "output" / "pdf" / name).read_bytes(), "application/pdf"))
                 for name in FIXTURES.values()]
        upload = post("/api/documents", data={"conversation_id": conversation}, files=files)
        ids = [item["file_id"] for item in upload["documents"]]
        live.check(len(ids) == 2 and len(set(ids)) == 2, "La prueba necesita exactamente los dos PDF ficticios.")
        def read_turn():
            data = post("/api/chat", json={"question": QUESTION, "conversation_id": conversation,
                                           "document_ids": ids})
            calls = []
            for call in (data.get("metrics") or {}).get("calls", []):
                if call.get("agent") == "leer_documento":
                    calls.append((call.get("reader_diagnostics") or {}).get("attempts", []))
            errors = [{"archivo": error.get("archivo"), "tipo": error.get("tipo")}
                      for error in data.get("document_errors", [])]
            result.setdefault("turnos", []).append({
                "trace_id": data.get("trace_id"), "lector": calls, "errores": errors,
                "fragmentos_de_política": len((data.get("policy") or {}).get("fragment_ids", [])),
            })
            live.report_save()
            return data, calls, errors

        data, calls, errors = read_turn()
        if errors and any(attempts and attempts[-1].get("http_status") == 503
                          and attempts[-1].get("provider_status") == "UNAVAILABLE" for attempts in calls):
            live.step(f"{port} · reintento de lectura en el mismo chat")
            data, _, errors = read_turn()
        result["trace_id"] = data.get("trace_id")
        live.check(not errors, "No se pudieron leer ambos PDFs; los intentos y archivos afectados están en el reporte.")
        live.check(set(data.get("tools", [])) >= {"leer_documento", "recuperar_politica_documental"}
                   and "data_steward" in data.get("agents", []),
                   "El turno no completó la recuperación de política con Steward.")
        policy = data.get("policy") or {}
        fragments = policy.get("fragment_ids", [])
        live.check(policy.get("producto") == "clasica" and fragments,
                   "Steward no devolvió fragmentos de la política de Clásica.")
        documents = data.get("documents", [])
        live.check(len(documents) == 2 and {doc["tipo"] for doc in documents} == set(FIXTURES)
                   and all(1 in doc["paginas_relevantes"] for doc in documents),
                   "El turno no reconoció la INE y el CFDI en sus primeras páginas.")
        view = data.get("attachment_view") or {}
        if view.get("style") != "preliminary_credit":
            view = data.get("document_view") or {}
        live.check(view.get("style") == "preliminary_credit", "El chat volvió al inventario.")
        rows = (data.get("derived") or {}).get("requisitos", [])
        references = view.get("references", [])
        evaluation = []
        for rule, (document_type, names) in FIELDS.items():
            selected = [row for row in rows if row["regla"] == rule]
            live.check(selected and all(row["estado"] == "cumple" for row in selected),
                       f"No se obtuvo una evaluación favorable respaldada para {rule}.")
            for row in selected:
                source = row.get("fuente_politica") or {}
                evidence = {field["campo"] for field in row["evidencia"]
                            if (field.get("fuente") or {}).get("archivo") == FIXTURES[document_type]
                            and (field.get("fuente") or {}).get("pagina") == 1}
                live.check(names <= evidence and source.get("fragment_id") in fragments and source.get("page")
                           and source.get("version") and source.get("consulted_at"),
                           f"Falta la cita documental o de política para {rule}.")
                evaluation.append({"regla": rule, "estado": row["estado"], "detalle": row["nota"],
                                   "archivo": FIXTURES[document_type], "página": 1,
                                   "página_política": source["page"], "campos": sorted(evidence)})
            live.check(any(FIXTURES[document_type] in cite and "1" in cite for cite in references),
                       f"La vista del chat no presenta la cita para {rule}.")
        missing = next((row for row in rows if row["regla"] == "antiguedad_comprobante_meses"), {})
        live.check(missing.get("motivo") == "documento_ausente" and missing.get("estado") == "no verificable"
                   and not missing.get("evidencia")
                   and any("domicilio" in row["label"].lower() and row["status"] == "falta documento"
                           for row in view.get("requirements", [])),
                   "El chat no indicó que falta el comprobante de domicilio.")
        result.update(ok=True, evaluación=evaluation, falta_comprobante_de_domicilio=True,
                      fragmentos_de_política=len(fragments), citas=references)
        live.report_save()
        post("/api/logout")


def listener_owner(port):
    lines = subprocess.check_output(["netstat", "-ano", "-p", "TCP"], text=True, errors="replace").splitlines()
    for line in lines:
        fields = line.split()
        if len(fields) == 5 and fields[:2] == ["TCP", f"127.0.0.1:{port}"] and fields[3] == "LISTENING":
            return int(fields[4])
    return None


def check_steward_db():
    """Confirma la conexión de lectura de Steward y el acceso al corpus."""
    import psycopg

    sys.path.insert(0, str(CUT))
    from banco_agentes.analytics import connect_readonly

    try:
        with connect_readonly() as database:
            database.execute("SELECT 1 FROM docs.fragments LIMIT 1").fetchone()
    except psycopg.Error as error:
        if error.sqlstate in {"28P01", "28000"} or "password authentication failed" in str(error).casefold():
            print("PostgreSQL rechazó la autenticación del rol de lectura de Steward. Revisa APP_DB_PASSWORD.")
        else:
            print(f"Falló la conexión o el permiso de lectura de Steward ({type(error).__name__}).")
        return 2
    except ValueError:
        print("La configuración PostgreSQL de solo lectura de Steward no es válida.")
        return 2
    print("PostgreSQL confirmó que el rol de lectura de Steward puede consultar el corpus.")
    return 0


def check_previous():
    """Identifica el proceso 8004 que se promovió en la verificación de compras."""
    owner = listener_owner(8004)
    live.check(owner is not None, "El proceso anterior de 8004 ya no está disponible.")
    history = json.loads(PREVIOUS_REPORT.read_text(encoding="utf-8"))
    launcher = int(history["puertos"]["8004"]["pid_arranque"])
    cmd = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={owner}'; "
        "if($p -and $p.Name -eq 'python.exe' -and "
        "$p.CommandLine -match 'banco-restore-compras-live\\.py\"?\\s+--serve\\s+8004(?:\\s|$)') "
        "{ Write-Output $p.ParentProcessId }"
    )
    parent = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", cmd], text=True).strip()
    live.check(parent.isdigit() and (int(parent) == launcher or owner == launcher),
               "8004 pertenece a otro proceso. Se conserva.")
    return owner


def main():
    live.report["registro"] = tempfile.mkdtemp(prefix="banco-clasica-live-")
    test = deployed = None
    previous_stopped = False
    try:
        live.step("comprobación del entorno de la terminal")
        env = os.environ.copy()
        live.check(bool(env.get("BANCO_CLIENT_DB_PASSWORD")), "Falta BANCO_CLIENT_DB_PASSWORD en esta terminal.")
        live.check(bool(env.get("GOOGLE_API_KEY")), "Falta GOOGLE_API_KEY para ejecutar Gemini.")
        if not env.get("APP_DB_PASSWORD"):
            env["APP_DB_PASSWORD"] = getpass("Contraseña PostgreSQL de lectura para Steward (entrada oculta): ")
        live.check(bool(env.get("APP_DB_PASSWORD")), "Falta APP_DB_PASSWORD para recuperar la política con Steward.")
        env["BANCO_USERS_FILE"] = str(Path(env.get("BANCO_USERS_FILE") or
                                           live.ORIGINAL / "config" / "auth_users.local.json").resolve())
        live.check(Path(env["BANCO_USERS_FILE"]).is_file(), "No se encontró el registro existente de usuarios.")
        live.report["registro_web"] = env["BANCO_USERS_FILE"]
        env["PYTHONIOENCODING"] = "utf-8"
        live.step("conexión de lectura de Steward con PostgreSQL")
        steward = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--check-steward-db"],
                                 cwd=CUT, env=env, capture_output=True, text=True, encoding="utf-8")
        live.report["steward_postgresql"] = {"ok": steward.returncode == 0}
        live.check(steward.returncode == 0, steward.stdout.strip() or "Falló el diagnóstico de conexión de Steward.")
        password = env.get("BANCO_CLIENTE_PASSWORD") if env.get("BANCO_CLIENTE_USERNAME", "cliente_a") == "cliente_a" else None
        password = password or getpass("Contraseña web de cliente_a (entrada oculta): ")
        live.check(bool(password), "Falta la contraseña web de cliente_a.")
        env.pop("BANCO_CLIENTE_PASSWORD", None)
        live.CUT, live.__file__ = CUT, __file__
        test = live.run_server(8005, env)
        try:
            verify_documents(8005, password)
        except live.VerificationError as doc_error:
            details = live.report["puertos"]["8005"]["documentos"]
            details.update(error=str(doc_error), etapa_fallo=live.report["etapa"])
            live.report_save()
        live.verify(8005, password)
        details = live.report["puertos"]["8005"]["documentos"]
        if not details.get("ok"):
            live.step(details["etapa_fallo"])
            raise live.VerificationError(details["error"])

        live.step("identificación de la instancia anterior en 8004")
        old_owner = check_previous()
        live.step("promoción a 8004 tras verificar los cuatro flujos en 8005")
        # Solo se detiene el listener exacto del corte anterior. La versión previa y su registro persisten.
        live.check(listener_owner(8004) == old_owner, "Cambió el proceso de 8004. Se conserva.")
        completed = subprocess.run(["taskkill", "/PID", str(old_owner), "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        live.check(completed.returncode == 0, "No se pudo detener la instancia anterior de 8004.")
        previous_stopped = True
        for _ in range(20):
            if not live.port_open(8004):
                break
            time.sleep(0.25)
        deployed = live.run_server(8004, env)
        verify_documents(8004, password)
        live.verify(8004, password)
        live.report["estado"] = "verificado"
        live.step("evaluación documental y tres flujos bancarios verificados en 8004")
        return 0
    except live.VerificationError as error:
        live.report["error"] = str(error)
    except (Exception, KeyboardInterrupt) as error:
        live.report["error"] = f"{type(error).__name__} en {live.report.get('etapa')}."
    finally:
        if test is not None:
            live.stop_own(test)
        if live.report["estado"] != "verificado" and previous_stopped:
            if deployed is not None:
                live.stop_own(deployed)
            live.report["etapa_fallo"] = live.report["etapa"]
            live.report["candidata_8004"] = live.report["puertos"].pop("8004", {})
            live.report["registro_candidata"] = live.report["registro"]
            live.report["registro"] = tempfile.mkdtemp(prefix="banco-clasica-reversion-")
            live.CUT, live.__file__ = PREVIOUS_CUT, str(PREVIOUS_HELPER)
            try:
                restored = live.run_server(8004, env)
                live.report["reversión"] = {"ok": True, "pid_arranque": restored.pid}
            except Exception as error:
                live.report["reversión"] = {"ok": False, "error": type(error).__name__}
    live.report["estado"] = "bloqueado"
    live.report_save()
    print(live.report["error"], flush=True)
    return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-steward-db"]:
        sys.exit(check_steward_db())
    if len(sys.argv) == 4 and sys.argv[1] == "--serve":
        live.CUT = CUT
        sys.exit(live.serve(int(sys.argv[2]), Path(sys.argv[3])))
    sys.exit(main())
