"""Symmetric encryption for per-user secrets (Anthropic API keys) at rest.

Keys are encrypted with Fernet, derived deterministically from the app's
``JOBFLOW_SECRET_KEY`` env var (the same secret that signs Flask sessions). We
never store or log the plaintext key; the encrypted bytes live in
``user_profiles.ai_api_key_enc`` and are only decrypted momentarily inside the
server-side scorer.

Named ``crypto`` (not ``secrets``) so it doesn't shadow Python's stdlib
``secrets`` module, which we use elsewhere for pairing-token generation.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    secret = os.environ.get("JOBFLOW_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "JOBFLOW_SECRET_KEY is not set — cannot encrypt/decrypt user API keys."
        )
    # Derive a valid 32-byte urlsafe-base64 Fernet key from an arbitrary secret.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt a plaintext secret to opaque bytes for storage."""
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(token: bytes | None) -> str | None:
    """Decrypt stored bytes back to plaintext. Returns None on empty/invalid
    input (e.g. if JOBFLOW_SECRET_KEY changed after the key was stored).
    """
    if not token:
        return None
    try:
        return _fernet().decrypt(bytes(token)).decode()
    except InvalidToken:
        return None
