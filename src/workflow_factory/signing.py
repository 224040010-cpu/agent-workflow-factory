from __future__ import annotations

import base64
import binascii
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .util import read_json, write_json


ALGORITHM = "Ed25519"
CANONICALIZATION = "AWF-CANONICAL-JSON-v1"
TRUSTED_KEY_STATUSES = {"active", "retired"}
KNOWN_KEY_STATUSES = TRUSTED_KEY_STATUSES | {"revoked"}


def canonical_json(value: Any) -> bytes:
    """Canonical encoding for signature statements owned by this repository."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} is not valid base64") from exc


def _public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def key_id(public_key: Ed25519PublicKey) -> str:
    return "sha256:" + hashlib.sha256(_public_bytes(public_key)).hexdigest()


def artifact_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def generate_signing_key(
    private_key_path: Path,
    trust_store_path: Path,
    publisher: str,
) -> dict:
    if not publisher.strip():
        raise ValueError("publisher must not be empty")
    if private_key_path.exists():
        raise ValueError(f"Refusing to overwrite private key: {private_key_path}")
    trust = (
        read_json(trust_store_path)
        if trust_store_path.is_file()
        else {"schema_version": "1.0.0", "keys": []}
    )
    if trust.get("schema_version") != "1.0.0" or not isinstance(trust.get("keys"), list):
        raise ValueError("Unsupported trust store")
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    record = {
        "key_id": key_id(public_key),
        "publisher": publisher,
        "algorithm": ALGORITHM,
        "status": "active",
        "public_key": _b64encode(_public_bytes(public_key)),
    }
    if any(item.get("key_id") == record["key_id"] for item in trust["keys"]):
        raise ValueError(f"Trust store already contains key: {record['key_id']}")
    trust["keys"].append(record)
    trust["keys"].sort(key=lambda item: item["key_id"])
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        private_key_path.chmod(0o600)
    except OSError:
        pass
    write_json(trust_store_path, trust)
    return copy.deepcopy(record)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Signing key must be Ed25519")
    return key


def sign_artifact(
    artifact_path: Path,
    private_key_path: Path,
    signature_path: Path,
    publisher: str,
    issued_at: str | None = None,
) -> dict:
    if not artifact_path.is_file():
        raise ValueError(f"Artifact does not exist: {artifact_path}")
    private_key = load_private_key(private_key_path)
    statement = {
        "schema_version": "1.0.0",
        "subject": {
            "name": artifact_path.name,
            "digest": artifact_digest(artifact_path),
            "media_type": "application/json",
        },
        "publisher": publisher,
        "key_id": key_id(private_key.public_key()),
        "algorithm": ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
    }
    envelope = {
        "statement": statement,
        "signature": _b64encode(private_key.sign(canonical_json(statement))),
    }
    write_json(signature_path, envelope)
    return copy.deepcopy(envelope)


def _trusted_key(trust_store: dict, wanted_key_id: str, publisher: str) -> Ed25519PublicKey:
    if trust_store.get("schema_version") != "1.0.0":
        raise ValueError("Unsupported trust store schema_version")
    keys = trust_store.get("keys")
    if not isinstance(keys, list):
        raise ValueError("Trust store keys must be an array")
    matches = [item for item in keys if item.get("key_id") == wanted_key_id]
    if len(matches) != 1:
        raise ValueError(f"Signature key is not uniquely trusted: {wanted_key_id}")
    record = matches[0]
    if record.get("publisher") != publisher:
        raise ValueError("Signature publisher does not match trusted key owner")
    if record.get("algorithm") != ALGORITHM:
        raise ValueError("Trusted key algorithm is not Ed25519")
    status = record.get("status")
    if status not in KNOWN_KEY_STATUSES:
        raise ValueError(f"Unknown trusted key status: {status}")
    if status not in TRUSTED_KEY_STATUSES:
        raise ValueError(f"Signature key is not usable: {status}")
    raw = _b64decode(record.get("public_key"), "trusted public key")
    if len(raw) != 32:
        raise ValueError("Trusted Ed25519 public key must be 32 bytes")
    public_key = Ed25519PublicKey.from_public_bytes(raw)
    if key_id(public_key) != wanted_key_id:
        raise ValueError("Trusted public key does not match key_id")
    return public_key


def verify_artifact(
    artifact_path: Path,
    signature_path: Path,
    trust_store_path: Path,
    expected_publisher: str,
) -> dict:
    envelope = read_json(signature_path)
    if set(envelope) != {"statement", "signature"}:
        raise ValueError("Signature envelope must contain exactly statement and signature")
    statement = envelope.get("statement")
    if not isinstance(statement, dict) or set(statement) != {
        "schema_version",
        "subject",
        "publisher",
        "key_id",
        "algorithm",
        "canonicalization",
        "issued_at",
    }:
        raise ValueError("Signature statement has an invalid shape")
    if statement.get("schema_version") != "1.0.0":
        raise ValueError("Unsupported signature statement schema_version")
    if statement.get("algorithm") != ALGORITHM:
        raise ValueError("Signature algorithm must be Ed25519")
    if statement.get("canonicalization") != CANONICALIZATION:
        raise ValueError("Unsupported signature canonicalization")
    if statement.get("publisher") != expected_publisher:
        raise ValueError("Artifact publisher differs from the required publisher")
    subject = statement.get("subject")
    if not isinstance(subject, dict) or set(subject) != {"name", "digest", "media_type"}:
        raise ValueError("Signature subject has an invalid shape")
    if subject.get("name") != artifact_path.name:
        raise ValueError("Signature subject name differs from artifact")
    if subject.get("media_type") != "application/json":
        raise ValueError("Signature subject media_type must be application/json")
    actual_digest = artifact_digest(artifact_path)
    if subject.get("digest") != actual_digest:
        raise ValueError("Artifact digest does not match signature subject")
    public_key = _trusted_key(
        read_json(trust_store_path), statement.get("key_id"), expected_publisher
    )
    signature = _b64decode(envelope.get("signature"), "signature")
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    try:
        public_key.verify(signature, canonical_json(statement))
    except InvalidSignature as exc:
        raise ValueError("Artifact signature verification failed") from exc
    return {
        "result": "PASS",
        "artifact": artifact_path.name,
        "digest": actual_digest,
        "publisher": expected_publisher,
        "key_id": statement["key_id"],
        "key_status": next(
            item["status"]
            for item in read_json(trust_store_path)["keys"]
            if item["key_id"] == statement["key_id"]
        ),
    }
