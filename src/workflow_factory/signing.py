from __future__ import annotations

import base64
import binascii
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol

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
KEY_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class SigningProvider(Protocol):
    """Provider boundary for file keys today and KMS/HSM implementations later."""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> bytes: ...


class FileEd25519SigningProvider:
    def __init__(self, private_key_path: Path):
        self._private_key = load_private_key(private_key_path)

    @property
    def key_id(self) -> str:
        return key_id(self._private_key.public_key())

    def sign(self, payload: bytes) -> bytes:
        return self._private_key.sign(payload)


class Pkcs11Ed25519SigningProvider:
    """Sign with an Ed25519 private key that never leaves a PKCS#11 token."""

    def __init__(
        self,
        module_path: str,
        token_label: str,
        key_label: str,
        trusted_key_id: str,
        pin_env: str = "AWF_PKCS11_PIN",
        key_object_id: bytes | None = None,
        *,
        _api: Any | None = None,
    ):
        if not module_path.strip():
            raise ValueError("PKCS#11 module path must not be empty")
        if not token_label.strip() or not key_label.strip():
            raise ValueError("PKCS#11 token and key labels must not be empty")
        if not KEY_ID_PATTERN.fullmatch(trusted_key_id):
            raise ValueError("PKCS#11 trusted key_id must be sha256:<64 lowercase hex>")
        if not pin_env.strip():
            raise ValueError("PKCS#11 PIN environment variable name must not be empty")
        self.module_path = module_path
        self.token_label = token_label
        self.key_label = key_label
        self.pin_env = pin_env
        self.key_object_id = key_object_id
        self._key_id = trusted_key_id
        self._api = _api

    @property
    def key_id(self) -> str:
        return self._key_id

    def _module(self) -> Any:
        if self._api is not None:
            return self._api
        try:
            self._api = importlib.import_module("pkcs11")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PKCS#11 signing requires the 'python-pkcs11' optional dependency"
            ) from exc
        return self._api

    def sign(self, payload: bytes) -> bytes:
        pin = os.environ.get(self.pin_env)
        if pin is None or not pin:
            raise RuntimeError(
                f"PKCS#11 user PIN is missing from environment variable {self.pin_env}"
            )
        api = self._module()
        library = api.lib(self.module_path)
        token = library.get_token(token_label=self.token_label)
        lookup: dict[str, Any] = {
            "object_class": api.ObjectClass.PRIVATE_KEY,
            "label": self.key_label,
        }
        if self.key_object_id is not None:
            lookup["id"] = self.key_object_id
        with token.open(user_pin=pin) as session:
            private_key = session.get_key(**lookup)
            signature = private_key.sign(payload, mechanism=api.Mechanism.EDDSA)
        result = bytes(signature)
        if len(result) != 64:
            raise RuntimeError("PKCS#11 Ed25519 signature must be 64 bytes")
        return result


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


def _provider_signature(provider: SigningProvider, statement: dict) -> str:
    signature = bytes(provider.sign(canonical_json(statement)))
    if len(signature) != 64:
        raise ValueError("Ed25519 signing provider must return a 64-byte signature")
    return _b64encode(signature)


def _public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def key_id(public_key: Ed25519PublicKey) -> str:
    return "sha256:" + hashlib.sha256(_public_bytes(public_key)).hexdigest()


def artifact_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def payload_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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


def register_signing_public_key(
    public_key_path: Path,
    trust_store_path: Path,
    publisher: str,
    status: str = "active",
) -> dict:
    """Register an exported Ed25519 public key without importing private material."""

    if not publisher.strip():
        raise ValueError("publisher must not be empty")
    if status not in KNOWN_KEY_STATUSES:
        raise ValueError(f"Unknown trusted key status: {status}")
    public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("Registered signing public key must be Ed25519")
    trust = (
        read_json(trust_store_path)
        if trust_store_path.is_file()
        else {"schema_version": "1.0.0", "keys": []}
    )
    if trust.get("schema_version") != "1.0.0" or not isinstance(trust.get("keys"), list):
        raise ValueError("Unsupported trust store")
    record = {
        "key_id": key_id(public_key),
        "publisher": publisher,
        "algorithm": ALGORITHM,
        "status": status,
        "public_key": _b64encode(_public_bytes(public_key)),
    }
    if any(item.get("key_id") == record["key_id"] for item in trust["keys"]):
        raise ValueError(f"Trust store already contains key: {record['key_id']}")
    trust["keys"].append(record)
    trust["keys"].sort(key=lambda item: item["key_id"])
    write_json(trust_store_path, trust)
    return copy.deepcopy(record)


def generate_root_key(
    private_key_path: Path,
    public_key_path: Path,
    publisher: str = "agent-workflow-factory-trust-root",
) -> dict:
    if private_key_path.exists() or public_key_path.exists():
        raise ValueError("Refusing to overwrite trust root key material")
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    record = {
        "schema_version": "1.0.0",
        "key_id": key_id(public_key),
        "publisher": publisher,
        "algorithm": ALGORITHM,
        "status": "active",
        "public_key": _b64encode(_public_bytes(public_key)),
    }
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
    write_json(public_key_path, record)
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
    return sign_artifact_with_provider(
        artifact_path,
        FileEd25519SigningProvider(private_key_path),
        signature_path,
        publisher,
        issued_at,
    )


def sign_artifact_with_provider(
    artifact_path: Path,
    provider: SigningProvider,
    signature_path: Path,
    publisher: str,
    issued_at: str | None = None,
) -> dict:
    if not artifact_path.is_file():
        raise ValueError(f"Artifact does not exist: {artifact_path}")
    statement = {
        "schema_version": "1.0.0",
        "subject": {
            "name": artifact_path.name,
            "digest": artifact_digest(artifact_path),
            "media_type": "application/json",
        },
        "publisher": publisher,
        "key_id": provider.key_id,
        "algorithm": ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
    }
    envelope = {
        "statement": statement,
        "signature": _provider_signature(provider, statement),
    }
    write_json(signature_path, envelope)
    return copy.deepcopy(envelope)


def sign_json_value(
    value: Any,
    subject_name: str,
    provider: SigningProvider,
    publisher: str,
    issued_at: str | None = None,
) -> dict:
    """Create an embedded detached-style envelope for one canonical JSON value."""

    if not subject_name.strip():
        raise ValueError("Signature subject name must not be empty")
    payload = canonical_json(value)
    statement = {
        "schema_version": "1.0.0",
        "subject": {
            "name": subject_name,
            "digest": payload_digest(payload),
            "media_type": "application/json",
        },
        "publisher": publisher,
        "key_id": provider.key_id,
        "algorithm": ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
    }
    return {
        "statement": statement,
        "signature": _provider_signature(provider, statement),
    }


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


def _key_from_record(record: dict, expected_publisher: str) -> Ed25519PublicKey:
    if record.get("schema_version") != "1.0.0":
        raise ValueError("Unsupported root key schema_version")
    if record.get("publisher") != expected_publisher:
        raise ValueError("Root key publisher differs from required publisher")
    if record.get("algorithm") != ALGORITHM or record.get("status") != "active":
        raise ValueError("Root key must be an active Ed25519 key")
    raw = _b64decode(record.get("public_key"), "root public key")
    if len(raw) != 32:
        raise ValueError("Root Ed25519 public key must be 32 bytes")
    public_key = Ed25519PublicKey.from_public_bytes(raw)
    if key_id(public_key) != record.get("key_id"):
        raise ValueError("Root public key does not match key_id")
    return public_key


def _verify_envelope(
    artifact_path: Path,
    signature_path: Path,
    expected_publisher: str,
    public_key_resolver: Any,
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
    public_key, key_status = public_key_resolver(statement)
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
        "key_status": key_status,
    }


def verify_artifact(
    artifact_path: Path,
    signature_path: Path,
    trust_store_path: Path,
    expected_publisher: str,
) -> dict:
    trust_store = read_json(trust_store_path)

    def resolve(statement: dict) -> tuple[Ed25519PublicKey, str]:
        public_key = _trusted_key(
            trust_store, statement.get("key_id"), expected_publisher
        )
        status = next(
            item["status"]
            for item in trust_store["keys"]
            if item["key_id"] == statement["key_id"]
        )
        return public_key, status

    return _verify_envelope(
        artifact_path, signature_path, expected_publisher, resolve
    )


def verify_json_value(
    value: Any,
    envelope: Any,
    subject_name: str,
    trust_store_path: Path,
    expected_publisher: str,
) -> dict:
    """Verify a canonical JSON value against an embedded signature envelope."""

    if not isinstance(envelope, dict) or set(envelope) != {"statement", "signature"}:
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
    if subject.get("name") != subject_name:
        raise ValueError("Signature subject name differs from JSON value")
    if subject.get("media_type") != "application/json":
        raise ValueError("Signature subject media_type must be application/json")
    actual_digest = payload_digest(canonical_json(value))
    if subject.get("digest") != actual_digest:
        raise ValueError("JSON value digest does not match signature subject")
    trust_store = read_json(trust_store_path)
    public_key = _trusted_key(
        trust_store, statement.get("key_id"), expected_publisher
    )
    signature = _b64decode(envelope.get("signature"), "signature")
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    try:
        public_key.verify(signature, canonical_json(statement))
    except InvalidSignature as exc:
        raise ValueError("JSON value signature verification failed") from exc
    status = next(
        item["status"]
        for item in trust_store["keys"]
        if item["key_id"] == statement["key_id"]
    )
    return {
        "result": "PASS",
        "artifact": subject_name,
        "digest": actual_digest,
        "publisher": expected_publisher,
        "key_id": statement["key_id"],
        "key_status": status,
    }


def verify_trust_store(
    trust_store_path: Path,
    signature_path: Path,
    root_public_key_path: Path,
    expected_publisher: str = "agent-workflow-factory-trust-root",
) -> dict:
    record = read_json(root_public_key_path)
    root_key = _key_from_record(record, expected_publisher)

    def resolve(statement: dict) -> tuple[Ed25519PublicKey, str]:
        if statement.get("key_id") != record["key_id"]:
            raise ValueError("Trust store signature does not use configured root key")
        return root_key, "root"

    return _verify_envelope(
        trust_store_path, signature_path, expected_publisher, resolve
    )
