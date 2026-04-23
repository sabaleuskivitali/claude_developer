"""
generate_key.py — Run ONCE to create the extension signing key.

Usage:
    python tools/generate_key.py

Outputs:
    tools/extension.pem          — private key (add to .gitignore!)
    tools/extension-id.txt       — 32-char extension ID (commit this)

After running:
    1. Copy the base64 output to GitHub Secret: EXTENSION_PRIVATE_KEY
    2. Commit extension-id.txt (the ID never changes as long as you keep the key)
    3. NEVER commit extension.pem
"""

import base64
import hashlib
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("Install: pip install cryptography")
    sys.exit(1)

ROOT = Path(__file__).parent.parent


def pub_key_to_extension_id(pub_der: bytes) -> str:
    """Chrome extension ID: SHA256(pub_der)[:16] → nibbles mapped to a-p."""
    h = hashlib.sha256(pub_der).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in h)


def main():
    pem_path = ROOT / "tools" / "extension.pem"
    id_path  = ROOT / "tools" / "extension-id.txt"

    if pem_path.exists():
        print(f"Key already exists at {pem_path}")
        print("Delete it first if you want to regenerate (WARNING: this changes the extension ID!)")
        sys.exit(1)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    pem_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pem_path.write_bytes(pem_bytes)

    pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    ext_id = pub_key_to_extension_id(pub_der)
    id_path.write_text(ext_id + "\n")

    b64 = base64.b64encode(pem_bytes).decode()

    print(f"\n✅ Key generated")
    print(f"   Extension ID : {ext_id}")
    print(f"   PEM saved to : {pem_path}  ← DO NOT COMMIT")
    print(f"\n── GitHub Secret ─────────────────────────────────────────")
    print(f"Name : EXTENSION_PRIVATE_KEY")
    print(f"Value:")
    print(b64)
    print(f"──────────────────────────────────────────────────────────")
    print(f"\nAdd {pem_path.name} to .gitignore, then commit extension-id.txt")


if __name__ == "__main__":
    main()
