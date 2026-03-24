"""Fernet encryption for OAuth tokens at rest."""
from cryptography.fernet import Fernet
from app.config import settings

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.FERNET_KEY
        if not key:
            key = Fernet.generate_key().decode()
            print(f"WARNING: No FERNET_KEY set. Generated ephemeral key. Set FERNET_KEY={key} in .env")
        if isinstance(key, str):
            key = key.encode()
        _fernet = Fernet(key)
    return _fernet


def encrypt_token(token: str) -> str:
    if not token:
        return ""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    if not encrypted:
        return ""
    return _get_fernet().decrypt(encrypted.encode()).decode()
