"""Verifica dos turnos en Chrome con la aplicación y Plotly reales y datos simulados."""
import argparse
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import uvicorn
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from test_graph import WorkflowModel, simulated_query
from web_fixtures import ACCOUNTS, create_app


def until(action, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = action()
            if value:
                return value
        except OSError:
            pass
        time.sleep(.1)
    raise TimeoutError("No se completó el arranque del servidor o de Chrome.")


def run_browser(chrome, url, profile, stderr):
    process = subprocess.Popen([
        chrome, "--headless=new", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "--disable-background-networking", "--disable-gpu",
        "--no-first-run", "--window-size=1200,900", url,
    ], stdout=subprocess.DEVNULL, stderr=stderr)
    try:
        port_file = profile / "DevToolsActivePort"
        port = until(lambda: port_file.read_text(encoding="ascii")).splitlines()[0]

        def tabs():
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                return next((tab for tab in json.load(response) if tab.get("url") == url), None)

        tab = until(tabs)
        with connect(tab["webSocketDebuggerUrl"]) as connection:
            def evaluate(expression):
                connection.send(json.dumps({
                    "id": 1, "method": "Runtime.evaluate",
                    "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
                }))
                while True:
                    response = json.loads(connection.recv(timeout=45))
                    if response.get("id") == 1:
                        return response

            def ready():
                response = evaluate("document.readyState === 'complete' && Boolean(window.Plotly)")
                return response.get("result", {}).get("result", {}).get("value")

            until(ready)
            login = {"username": "analista", "password": ACCOUNTS["analista"][0]}
            response = evaluate(CHECK_TURNS.replace("TEST_LOGIN", json.dumps(login)))
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        evaluation = response["result"]
        if "exceptionDetails" in evaluation:
            error = evaluation["exceptionDetails"].get("exception", {}).get("description", "Falló la prueba de Chrome")
            raise AssertionError(error)
        return evaluation["result"]["value"]
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


CHECK_TURNS = r"""(async () => {
    const wait = async (probe, name) => {
        for (let i = 0; i < 300; i++) {
            const value = probe();
            if (value) return value;
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        throw new Error('No apareció ' + name);
    };
    const check = (value, name) => { if (!value) throw new Error(name); };
    const login = TEST_LOGIN;
    await wait(() => document.querySelector('#username'), 'el acceso');
    document.querySelector('#username').value = login.username;
    document.querySelector('#password').value = login.password;
    document.querySelector('#login-form').requestSubmit();
    await wait(() => !document.querySelector('#chat-view').hidden, 'el chat');
    const ask = async (question, count) => {
        document.querySelector('#question').value = question;
        document.querySelector('#chat-form').requestSubmit();
        return wait(() => {
            const cards = document.querySelectorAll('.message.assistant');
            const card = cards[count - 1];
            if (!card || !card.querySelector('.js-plotly-plot .main-svg .legend')) return;
            const plot = card.querySelector('.js-plotly-plot');
            const caption = card.querySelector('figcaption');
            const legend = plot.querySelector('.legend').getBoundingClientRect();
            const gridTop = plot.getBoundingClientRect().top + plot._fullLayout._size.t;
            check(legend.top >= caption.getBoundingClientRect().bottom && legend.bottom <= gridTop + 2, 'Leyenda fuera de la parte superior');
            check(card.querySelector('.response-sources')?.textContent.includes('Fuentes consultadas'), 'Faltan fuentes');
            check(card.querySelector('details.response-sources')?.open === false, 'Fuentes debe empezar plegado');
            check(!card.querySelector('details.query-details'), 'El panel técnico no debe aparecer');
            check(/^Respondió en \d+\.\d+ s$/.test(card.querySelector('.response-time')?.textContent), 'Falta el tiempo total');
            return {
                answer: card.querySelector('.answer').textContent,
                type: plot.data.map(trace => trace.type),
                series: plot.data.map(({name, x, y, marker}) => ({name, x, y, color: marker.color})),
                legend: Array.from(plot.querySelectorAll('.legendtext'), item => item.textContent),
                legendAboveGrid: true, barmode: plot._fullLayout.barmode,
            };
        }, 'la gráfica del turno ' + count);
    };
    const first = await ask('Grafica en línea la evolución de CHEQUES y AHORRO de enero a junio de 2026', 1);
    const second = await ask('ponla en barras agrupadas', 2);
    check(first.answer.startsWith('De enero a junio de 2026.')
        && first.answer.includes('CHEQUES pasó de 50,160,000 a 53,240,000.')
        && first.answer.includes('AHORRO pasó de 24,870,000 a 26,400,000.'), 'Texto de evolución incorrecto: ' + first.answer);
    check(second.answer.startsWith('Cambié la gráfica a barras agrupadas con los mismos datos.'), 'Texto del cambio incorrecto: ' + second.answer);
    check(first.type.join() === 'scatter,scatter' && second.type.join() === 'bar,bar', 'Tipos incorrectos');
    check(second.barmode === 'group' && JSON.stringify(first.series) === JSON.stringify(second.series), 'Cambió el modo o el conjunto de datos');
    check(first.legend.join() === 'CHEQUES,AHORRO' && second.legend.join() === first.legend.join(), 'Leyenda de productos incorrecta');
    return {plotly: Plotly.version, turns: [first, second]};
})()"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args = parser.parse_args()

    def query(plan, *, quality=False):
        result = simulated_query(plan, quality=quality)
        values = {"CHEQUES": (50160000, 53240000), "AHORRO": (24870000, 26400000)}
        start, end = values[plan["producto"]]
        for index, row in enumerate(result["rows"]):
            row["valor"] = str(start + (end - start) * index // 5)
        return result

    with tempfile.TemporaryDirectory(prefix="plotly-browser-", dir=ROOT) as directory, socket.socket() as listener, \
            patch("banco_agentes.general_agents.create_model", side_effect=lambda name, *a, **k: WorkflowModel(model="simulado", agent=name)), \
            patch("banco_agentes.general_agents.execute_plan", side_effect=query) as analytics:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/"
        server = uvicorn.Server(uvicorn.Config(create_app(audit_path=None), log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            until(lambda: server.started)
            with urlopen(url + "assets/plotly.min.js", timeout=3) as response:
                assert response.status == 200
            with open(ROOT / "verification-plotly-summary-chrome.stderr.log", "wb") as stderr:
                result = run_browser(args.chrome, url, Path(directory) / "chrome", stderr)
            assert analytics.call_count == 2, f"Se esperaban solo dos consultas iniciales: {analytics.call_count}"
            result["consultas_analytics"] = analytics.call_count
            (ROOT / "verification-plotly-summary-browser.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            print("Chrome y Plotly verificaron ambos turnos. Analytics ejecutó dos consultas.")
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
