"""Small encryption helper for secrets that must be recoverable by the
panel (guest root passwords). The Fernet key is derived from SECRET_KEY,
which itself lives only in the environment / 0600 secret file."""
from __future__ import annotations
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))


def encrypt_text(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_text(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
