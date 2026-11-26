"""Ejecuta el renderizador del chat en Chromium con resultados contrastados en SQL."""
import argparse
import html
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "banco_agentes" / "web_assets"
PAGE = """<!doctype html><html lang="es"><meta charset="utf-8">
<title>Banco Agentes · Verificación de gráficas</title>
<link rel="stylesheet" href="__STYLE__">
<link rel="stylesheet" href="__CHART_STYLE__">
<main id="figures"></main><output id="checks">PENDIENTE</output>
<script src="__SCRIPT__"></script><script src="__RESPONSE_SCRIPT__"></script><script>
const specs = __CHARTS__;
const container = document.getElementById("figures");
for (const spec of specs) {
    const card = document.createElement("article");
    renderCharts(card, [spec]);
    renderResponseExtras(card, {
        charts: [spec], sources: [{label: "Tabla de origen",location: spec.source},
            ...spec.citations.map(citation => ({label: "Script SQL",location: citation}))],
        total_seconds: 0, agents: [], tools: []
    });
    container.append(card);
}
const checks = [...container.querySelectorAll("figure")].map((figure, index) => {
    const spec = specs[index];
    const svg = figure.querySelector('svg[role="img"]');
    const table = figure.querySelector("table");
    const marks = svg.querySelectorAll(["barras", "histograma"].includes(spec.type) ? "rect" : "circle");
    const rows = [...table.querySelectorAll("tbody tr")];
    const passed = svg.getBoundingClientRect().width > 0 &&
        marks.length === spec.points.length && rows.length === spec.points.length &&
        rows.every((row, i) => row.lastElementChild.textContent === displayDecimal(spec.points[i].y)) &&
        table.caption.textContent.length > 0 && figure.parentElement.querySelectorAll(".response-sources li").length === spec.citations.length + 1 &&
        !figure.parentElement.querySelector(".query-details").open;
    return {type: spec.type, rows: rows.length, marks: marks.length, passed};
});
document.getElementById("checks").textContent = JSON.stringify({
    passed: checks.length === 4 && checks.every(check => check.passed), checks, browser: navigator.userAgent
});
</script></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("browser", type=Path, help="Ruta del ejecutable local de Chrome o Edge")
    args = parser.parse_args()
    start = time.perf_counter()
    report = {"passed": False, "executable": args.browser.name}
    try:
        sql = json.loads((ROOT / "verification-general-sql.json").read_text(encoding="utf-8"))
        assert sql["passed"], "Primero ejecuta verify_general_sql.py"
        page = PAGE
        for token, file in (("__STYLE__", "style.css"), ("__CHART_STYLE__", "charts.css"), ("__SCRIPT__", "charts.js"), ("__RESPONSE_SCRIPT__", "responses.js")):
            page = page.replace(token, (ASSETS / file).as_uri())
        page = page.replace("__CHARTS__", json.dumps(sql["charts"], ensure_ascii=False).replace("<", "\\u003c"))
        with tempfile.TemporaryDirectory(prefix="banco-browser-", dir=ROOT) as folder:
            path = Path(folder).resolve()
            assert path.is_relative_to(ROOT)
            fixture = path / "graficas.html"
            fixture.write_text(page, encoding="utf-8")
            run = subprocess.run([
                str(args.browser), "--headless=new", "--disable-gpu", "--disable-gpu-compositing",
                "--disable-software-rasterizer", "--disable-background-networking", "--disable-component-update",
                "--no-first-run", "--no-default-browser-check", f"--user-data-dir={path / 'profile'}",
                "--dump-dom", fixture.as_uri(),
            ], capture_output=True, encoding="utf-8", errors="replace", timeout=40)
            report["exit_code"] = run.returncode
            match = re.search(r'<output id="checks">(.*?)</output>', run.stdout, re.S)
            if not match or run.returncode:
                report["stderr_tail"] = run.stderr[-2200:]
                raise RuntimeError("El navegador no devolvió el DOM renderizado.")
            report.update(json.loads(html.unescape(match[1])))
            assert report["passed"], report
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["total_seconds"] = time.perf_counter() - start
        (ROOT / "verification-general-browser.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
