"""Encrypts WarGear API keys at rest."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class KeyBox:
    def __init__(self, secret: str):
        if not secret:
            raise ValueError(
                "JOSHUA_SECRET_KEY is not set. Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
        self._fernet = Fernet(secret.encode())

    def seal(self, api_key: str) -> bytes:
        return self._fernet.encrypt(api_key.encode())

    def open(self, sealed: bytes) -> str:
        try:
            return self._fernet.decrypt(sealed).decode()
        except InvalidToken as e:
            raise ValueError("stored API key could not be decrypted; was JOSHUA_SECRET_KEY changed?") from e
