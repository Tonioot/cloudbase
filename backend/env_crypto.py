import json
import logging
import os

log = logging.getLogger("cloudbase.env_crypto")

_KEY_FILE = os.path.expanduser("~/.cloudbase/env_key")
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet
    os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        with open(_KEY_FILE, "wb") as f:
            f.write(key)
        try:
            os.chmod(_KEY_FILE, 0o600)
        except Exception:
            pass
        log.info("Generated new env encryption key at %s", _KEY_FILE)
    _fernet = Fernet(key)
    return _fernet


def encrypt_env(env_dict: dict) -> str:
    """Encrypt env vars dict → opaque string stored in Application.env_vars."""
    payload = json.dumps(env_dict).encode()
    return _get_fernet().encrypt(payload).decode()


def encrypt_text(value: str) -> str:
    """Encrypt a plain text value into an opaque token-safe string."""
    if value is None:
        return ""
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_env(ciphertext: str) -> dict:
    """
    Decrypt env vars back to dict.
    Falls back to plain json.loads for existing unencrypted records (backward compat).
    """
    if not ciphertext:
        return {}
    try:
        return json.loads(_get_fernet().decrypt(ciphertext.encode()))
    except Exception:
        pass
    try:
        return json.loads(ciphertext)
    except Exception:
        log.warning("env_vars could not be decrypted or parsed, returning empty dict")
        return {}


def decrypt_text(ciphertext: str, fallback_plaintext: bool = True) -> str:
    """
    Decrypt text encrypted by encrypt_text.
    If fallback_plaintext is True, legacy plaintext values are returned as-is.
    """
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        if fallback_plaintext:
            return ciphertext
        return ""
