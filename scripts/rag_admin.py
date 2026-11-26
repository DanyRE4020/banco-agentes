"""Administración local explícita; nunca lee archivos .env desde Python."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, data=None):
    exe = shutil.which("docker") or str(Path(os.environ["LOCALAPPDATA"])/"Programs/DockerDesktop/resources/bin/docker.exe")
    p = subprocess.run([exe, *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    if p.returncode:
        # No propagar stderr: puede contener parámetros sensibles de Compose/libpq.
        raise RuntimeError("docker_operation_failed")
    return p.stdout


def container():
    result = docker("compose", "--env-file", ".env.postgres", "ps", "-q", "postgres").decode().strip()
    if not result:
        raise RuntimeError("postgres_container_missing")
    return result


def sql(text, *, cid=None):
    command = 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v app_user="$APP_DB_USER"'
    return docker("exec", "-i", cid or container(), "sh", "-c", command, data=text.encode("utf-8")).decode("utf-8")


def business_signature():
    # Huella de TODAS las filas, sin imprimir registros individuales.
    return json.loads(sql("""SELECT json_build_object(
      'clientes', (SELECT md5(string_agg(md5(t::text), '' ORDER BY md5(t::text))) FROM dim.dim_cliente t),
      'productos', (SELECT md5(string_agg(md5(t::text), '' ORDER BY md5(t::text))) FROM dim.dim_producto t),
      'txn', (SELECT md5(string_agg(md5(t::text), '' ORDER BY md5(t::text))) FROM dim.dim_txn t),
      'saldos', (SELECT md5(string_agg(md5(t::text), '' ORDER BY md5(t::text))) FROM cfg.fact_sdos_captacoloca_m t),
      'movimientos', (SELECT md5(string_agg(md5(t::text), '' ORDER BY md5(t::text))) FROM cfg.fact_txn_captacoloca_d t));"""))


def backup():
    cid = container()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT/"backups"; folder.mkdir(exist_ok=True)
    name = f"banco-before-rag-{stamp}.dump"
    remote = "/tmp/"+name
    docker("exec", cid, "sh", "-c", f'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f {remote}')
    listing = docker("exec", cid, "pg_restore", "--list", remote)
    docker("exec", cid, "pg_restore", "--file=/dev/null", remote)
    docker("cp", f"{cid}:{remote}", str(folder/name))
    report = dict(file="backups/"+name, bytes=(folder/name).stat().st_size,
                  sha256=hashlib.sha256((folder/name).read_bytes()).hexdigest(),
                  archive_entries=len(listing.splitlines()), readable=True,
                  signature=business_signature(), postgres=sql("SELECT version();").strip())
    (ROOT/"verification-rag-backup.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup","migrate","signature"])
    args = parser.parse_args()
    if args.action == "backup":
        backup()
    elif args.action == "migrate":
        print(sql((ROOT/"docker/postgres/docs.sql").read_text(encoding="utf-8")))
        print("Migración docs aplicada.")
    else:
        print(json.dumps(business_signature(), indent=2))


if __name__ == "__main__":
    main()
