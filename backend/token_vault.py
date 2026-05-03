"""
Persistent, filesystem-backed store for saved GitHub tokens.
Tokens are kept in ~/.cloudbase/github_tokens.json (chmod 600).
Raw values are NEVER returned via the API — only hints (last 4 chars).
"""
import json
import os
import uuid
from typing import Optional

from env_crypto import encrypt_text, decrypt_text

_TOKENS_FILE = os.path.expanduser("~/.cloudbase/github_tokens.json")
_LEGACY_FILE = os.path.expanduser("~/.pdmanager/github_tokens.json")


def load() -> list[dict]:
    if not os.path.exists(_TOKENS_FILE) and os.path.exists(_LEGACY_FILE):
        try:
            with open(_LEGACY_FILE) as f:
                tokens = json.load(f)
            save(tokens)  # write to new location
            os.remove(_LEGACY_FILE)
            return tokens
        except Exception:
            pass
    try:
        with open(_TOKENS_FILE) as f:
            tokens = json.load(f)
    except Exception:
        return []

    normalized: list[dict] = []
    migrated = False
    for entry in tokens:
        if not isinstance(entry, dict):
            continue

        token_id = entry.get("id") or str(uuid.uuid4())
        label = str(entry.get("label") or "")
        raw_or_encrypted = str(entry.get("token") or "")
        if not raw_or_encrypted:
            continue

        decrypted = decrypt_text(raw_or_encrypted, fallback_plaintext=True)
        encrypted = raw_or_encrypted

        # Legacy records were stored as plaintext; re-save them encrypted.
        if decrypted == raw_or_encrypted:
            encrypted = encrypt_text(decrypted)
            migrated = True

        normalized.append({"id": token_id, "label": label, "token": encrypted})

    if migrated:
        save(normalized)

    return normalized


def save(tokens: list[dict]) -> None:
    os.makedirs(os.path.dirname(_TOKENS_FILE), exist_ok=True)
    with open(_TOKENS_FILE, "w") as f:
        json.dump(tokens, f)
    os.chmod(_TOKENS_FILE, 0o600)


def resolve(token_id: str) -> Optional[str]:
    """Return the raw token for a given ID, or None if not found."""
    for t in load():
        if t["id"] == token_id:
            token = t.get("token")
            if not token:
                return None
            return decrypt_text(token, fallback_plaintext=True)
    return None


def add(label: str, token: str) -> None:
    tokens = load()
    existing = next((t for t in tokens if t["label"] == label), None)
    encrypted = encrypt_text(token)
    if existing:
        existing["token"] = encrypted
    else:
        tokens.append({"id": str(uuid.uuid4()), "label": label, "token": encrypted})
    save(tokens)


def remove(token_id: str) -> None:
    save([t for t in load() if t["id"] != token_id])


def list_hints() -> list[dict]:
    """Return token list with only id, label, and last-4-char hint. Never raw values."""
    hints: list[dict] = []
    for t in load():
        decrypted = decrypt_text(t.get("token", ""), fallback_plaintext=True)
        hints.append({"id": t["id"], "label": t["label"], "token_hint": decrypted[-4:] if decrypted else ""})
    return hints
