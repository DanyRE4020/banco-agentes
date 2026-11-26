"""Crea o actualiza un usuario local mediante una contraseña ingresada sin eco."""
import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from banco_agentes.auth import password_record, read_users, users_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("rol", choices=("Cliente", "Empleado"))
    parser.add_argument("--num-cte", type=int)
    args = parser.parse_args()
    username = args.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", username):
        parser.error("Usuario inválido.")
    if args.rol == "Cliente":
        if args.num_cte is None or not 0 < args.num_cte <= 2147483647:
            parser.error("Cliente requiere --num-cte con un identificador válido.")
    elif args.num_cte is not None:
        parser.error("Empleado no recibe --num-cte.")
    password = getpass.getpass("Contraseña (mínimo 12 caracteres): ")
    if not 12 <= len(password) <= 128 or password != getpass.getpass("Repite la contraseña: "):
        parser.error("La longitud o confirmación de contraseña es inválida.")
    path = users_path()
    if path.suffix.lower() != ".json":
        parser.error("BANCO_USERS_FILE debe apuntar a un archivo JSON.")
    users = read_users(path)
    users[username] = {"rol": args.rol, "num_cte": args.num_cte, **password_record(password)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf8") as out:
        json.dump(users, out, ensure_ascii=False, indent=2)
    temporary.replace(path)
    print("Usuario guardado. Reinicia la aplicación para cargar su registro.")


if __name__ == "__main__":
    main()
