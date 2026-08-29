from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from workflow_factory.signing import (  # noqa: E402
    Pkcs11Ed25519SigningProvider,
    sign_json_value,
    verify_json_value,
)


REQUIRED_ENV = (
    "AWF_PKCS11_MODULE",
    "AWF_PKCS11_TOKEN_LABEL",
    "AWF_PKCS11_KEY_LABEL",
    "AWF_PKCS11_KEY_ID",
    "AWF_PKCS11_PIN",
    "AWF_PKCS11_TRUST_STORE",
)


@unittest.skipUnless(
    os.environ.get("AWF_PKCS11_LIVE_TEST") == "1"
    and all(os.environ.get(name) for name in REQUIRED_ENV),
    "requires AWF_PKCS11_LIVE_TEST=1 and a configured Ed25519 PKCS#11 token",
)
class Pkcs11LiveTest(unittest.TestCase):
    def test_token_signs_and_trust_store_verifies(self) -> None:
        provider = Pkcs11Ed25519SigningProvider(
            os.environ["AWF_PKCS11_MODULE"],
            os.environ["AWF_PKCS11_TOKEN_LABEL"],
            os.environ["AWF_PKCS11_KEY_LABEL"],
            os.environ["AWF_PKCS11_KEY_ID"],
        )
        value = {"purpose": "agent-workflow-factory-pkcs11-live-test"}
        envelope = sign_json_value(
            value,
            "pkcs11-live-test.json",
            provider,
            "agent-workflow-factory-runtime",
        )
        report = verify_json_value(
            value,
            envelope,
            "pkcs11-live-test.json",
            Path(os.environ["AWF_PKCS11_TRUST_STORE"]),
            "agent-workflow-factory-runtime",
        )
        self.assertEqual(report["result"], "PASS")


if __name__ == "__main__":
    unittest.main()
