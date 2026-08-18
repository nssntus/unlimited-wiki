"""Versioned, field-bound encryption for workspace model settings."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MODEL_ENCRYPTION_VERSION = 2
MODEL_FIELDS = frozenset({"base_url", "api_key"})


def _scope(workspace_id: str, field: str, version: int) -> str:
    if field not in MODEL_FIELDS:
        raise ValueError("invalid model setting field")
    if version == 1:
        return f"workspace:{workspace_id}:model"
    if version == MODEL_ENCRYPTION_VERSION:
        return f"workspace:{workspace_id}:model:{field}"
    raise ValueError("unsupported model setting encryption version")


def encrypt_model_value(
    cipher: AESGCM, plaintext: str, *, workspace_id: str, field: str,
) -> str:
    if not plaintext:
        return ""
    nonce = os.urandom(12)
    encrypted = cipher.encrypt(
        nonce,
        plaintext.encode("utf-8"),
        _scope(workspace_id, field, MODEL_ENCRYPTION_VERSION).encode("utf-8"),
    )
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_model_value(
    cipher: AESGCM,
    value: str,
    *,
    workspace_id: str,
    field: str,
    version: int,
) -> str:
    if not value:
        return ""
    raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    if len(raw) < 29:
        raise ValueError("encrypted model setting is truncated")
    return cipher.decrypt(
        raw[:12], raw[12:], _scope(workspace_id, field, version).encode("utf-8"),
    ).decode("utf-8")
