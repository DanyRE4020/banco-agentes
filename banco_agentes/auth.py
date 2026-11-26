"""Autenticación web local. El archivo contiene identidades y hashes de contraseña."""
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from .catalog import ROOT
from .governance import DemoSession


def users_path():
    return Path(os.getenv("BANCO_USERS_FILE", str(ROOT / "config" / "auth_users.local.json")))


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def password_record(password):
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "password_hash": _hash(password, salt).hex()}


def read_users(path):
    if path.suffix.lower() != ".json":
        raise ValueError("BANCO_USERS_FILE debe apuntar a un archivo JSON.")
    try:
        users = json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        return {}
    if not isinstance(users, dict):
        raise ValueError("El archivo de usuarios debe contener un objeto JSON.")
    return users


def authenticate(username, password, users):
    entry = users.get(username)
    salt, expected = b"\0" * 16, b"\0" * 32
    if entry is not None:
        try:
            salt = bytes.fromhex(entry["salt"])
            expected = bytes.fromhex(entry["password_hash"])
        except (KeyError, ValueError, TypeError):
            return None
        if len(salt) != 16 or len(expected) != 32:
            return None
    if not hmac.compare_digest(_hash(password, salt), expected) or entry is None:
        return None
    role, num_cte = entry.get("rol"), entry.get("num_cte")
    if role == "Cliente" and type(num_cte) is int and 0 < num_cte <= 2147483647:
        return DemoSession("Cliente", num_cte, principal=username)
    if role == "Empleado" and num_cte is None:
        return DemoSession("Analista", principal=username)
    return None


class SessionTokens:
    """Token firmado con referencia opaca a las claims y expiración del servidor."""
    def __init__(self):
        self._key = secrets.token_bytes(32)

    def _signature(self, value):
        return hmac.new(self._key, value.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self):
        nonce = secrets.token_hex(32)
        return f"{nonce}.{self._signature(nonce)}"

    def valid(self, token):
        if not token or len(token) != 129:
            return False
        nonce, separator, signature = token.partition(".")
        if not separator or not token.isascii():
            return False
        return hmac.compare_digest(self._signature(nonce), signature)

    def owner(self, identity):
        subject = json.dumps([identity.profile, identity.principal, identity.num_cte], ensure_ascii=True)
        return self._signature(subject)
