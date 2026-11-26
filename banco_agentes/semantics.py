"""Reglas verificables de los SQL cargados en PostgreSQL."""
from dataclasses import dataclass

DIM = "referencias/001_dimensiones_cliente_producto_txn (1).sql"
JAN = "referencias/002_generar_cuentas_y_saldos_enero_2026 (1).sql"
MOV = "referencias/003_generar_transacciones_y_saldos_feb_jun_2026 (1).sql"
BALANCE_OPENING_RULE = "Desde febrero, saldo_apertura toma el saldo al cierre del mes anterior."
BALANCE_OPENING_SOURCE = f"{MOV}:169-170,336-337"
PRODUCTS = ("AHORRO", "CHEQUES", "C_PERSONAL", "TDC")
SEGMENTS = ("MASIVO", "PREFERENTE", "PREMIUM")
# Campos poblados para cada cliente por el generador base, tanto PF como PM.
CUSTOMER_BASIC_FIELDS = ("num_cte", "fec_alta", "fec_nac", "tipo_persona", "genero_cte", "segmento_cte")
CUSTOMER_COMPLETENESS_DETAIL = "campos_basicos_detalle"


@dataclass(frozen=True)
class Dataset:
    table: str
    date_column: str | None
    month_column: str | None
    measure: str | None
    key: tuple[str, ...]
    fields: tuple[str, ...]
    sources: tuple[str, ...]
    grain: str


DATASETS = {
    "saldos": Dataset(
        "cfg.fact_sdos_captacoloca_m", "fec_informacion", "fec_informacion / 100", "saldo_mes",
        ("num_cta", "cve_producto", "fec_informacion"),
        ("num_cta", "num_cte", "cve_producto", "fec_alta", "cve_mes_alta",
         "saldo_apertura", "saldo_mes", "fec_informacion", "dominio"),
        (f"{JAN}:4-43", f"{MOV}:4-23", f"{MOV}:324-342"),
        "Una fila esperada por cuenta, producto y corte mensual. Enero a junio de 2026. "
        f"saldo_mes es el cierre. {BALANCE_OPENING_RULE} "
        "El crecimiento sintético de captación/colocación es 2 %/3 % en febrero y 1 %/2,2 % de marzo a junio. "
        "Los cierres son existencias: compara por mes, no los sumes entre meses."),
    "movimientos": Dataset(
        "cfg.fact_txn_captacoloca_d", "fec_txn", "cve_mes_txn", "saldo_txn",
        ("num_cta", "cve_producto", "cve_mes_txn", "cve_txn"),
        ("num_cta", "num_cte", "cve_producto", "fec_txn", "cve_mes_txn",
         "saldo_txn", "cve_txn", "desc_txn", "fec_informacion", "dominio"),
        (f"{MOV}:4-59", f"{MOV}:433-453", f"{DIM}:100-121"),
        "Febrero a junio de 2026, un movimiento por tipo aplicable, cuenta, producto y mes "
        "según el generador. saldo_txn guarda importes positivos. El efecto neto se "
        "calcula por producto y código: 100 depósito, 200 retiro, 300 pago, 400 compra, 450 disposición."),
    "clientes": Dataset(
        "dim.dim_cliente", None, None, None, ("num_cte",),
        ("num_cte", "fec_alta", "fec_nac", "tipo_persona", "genero_cte", "segmento_cte",
         "giro_empresa", "cve_territorio", "cve_dir", "cve_subdir", "cve_sucursal", "cve_promotor"),
        (f"{DIM}:12-55", f"{DIM}:72-79"),
        "Una fila por cliente, PK num_cte. PF y PM son persona física y moral. "
        "Es una dimensión estática; no representa una serie de altas."),
}

# Combinaciones explícitas del generador y de dim_txn. NULL marca código inesperado.
SIGNED = """CASE
 WHEN f.cve_producto IN ('AHORRO','CHEQUES') AND f.cve_txn=100 THEN f.saldo_txn
 WHEN f.cve_producto IN ('AHORRO','CHEQUES') AND f.cve_txn=200 THEN -f.saldo_txn
 WHEN f.cve_producto IN ('TDC','C_PERSONAL') AND f.cve_txn=300 THEN -f.saldo_txn
 WHEN f.cve_producto IN ('TDC','C_PERSONAL') AND f.cve_txn=450 THEN f.saldo_txn
 WHEN f.cve_producto='TDC' AND f.cve_txn=400 THEN f.saldo_txn END"""

RELATIONS = (
    f"Une hechos con dim.dim_cliente por num_cte y con dim.dim_producto por cve_producto; "
    f"las dimensiones tienen PK. Son relaciones de consulta, no FK declaradas. {DIM}:12-26,82-98. "
    f"dim.dim_txn carece de PK/UNIQUE. Para consultar su descripción usa cve_producto y cve_txn, "
    f"verificando unicidad antes de unir. {DIM}:5-6,100-121. "
    f"dominio en saldos cambia de clasificación entre enero y febrero; usa el producto "
    f"para comparar periodos. {JAN}:90-115; {MOV}:324-342."
)


def describe_dataset(name: str) -> dict:
    dataset = DATASETS.get(name)
    if dataset is None:
        return {"status": "unknown", "available": list(DATASETS)}
    return {"table": dataset.table, "grain": dataset.grain, "fields": dataset.fields,
            "expected_key": dataset.key, "relations": RELATIONS, "sources": dataset.sources,
            "units": "Importe sintético, moneda sin definir" if dataset.measure else "Clientes",
            "limitation": "El diccionario de tarjetas solo define variables. No es una tabla cargada."}
