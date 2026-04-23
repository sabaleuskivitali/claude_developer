"""
ECDSA-P256 signing for bootstrap profiles.

Private key path: env BOOTSTRAP_CA_KEY_PATH (default /app/certs/bootstrap_ca.pem).
If the key file does not exist, a new key pair is generated and saved.
The public key must be exported once and embedded in the agent binary at build time:
    python manage.py bootstrap export-pubkey
"""
import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bootstrap.schema import BootstrapProfile, SignedBootstrapProfile

_KEY_PATH_ENV = "BOOTSTRAP_CA_KEY_PATH"
_DEFAULT_KEY_PATH = "/app/certs/bootstrap_ca.pem"


def _key_path() -> Path:
    return Path(os.environ.get(_KEY_PATH_ENV, _DEFAULT_KEY_PATH))


def _load_or_generate() -> ec.EllipticCurvePrivateKey:
    path = _key_path()
    if path.exists():
        pem = path.read_bytes()
        return serialization.load_pem_private_key(pem, password=None)  # type: ignore[return-value]
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    import os as _os
    _os.chmod(path, 0o600)  # owner read/write only
    return key


def export_public_key_pem() -> str:
    """Return PEM of the CA public key — embed this in the agent binary."""
    return _load_or_generate().public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def sign_profile(profile: BootstrapProfile) -> SignedBootstrapProfile:
    key = _load_or_generate()
    canonical = profile.canonical_bytes()
    # DER-encoded ECDSA signature (Rfc3279DerSequence — matches .NET VerifyData default)
    sig = key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    return SignedBootstrapProfile(
        signed_data=base64.b64encode(canonical).decode(),
        signature=base64.b64encode(sig).decode(),
    )


def verify_signed_profile(signed: SignedBootstrapProfile) -> bool:
    try:
        pub = _load_or_generate().public_key()
        data = base64.b64decode(signed.signed_data)
        sig = base64.b64decode(signed.signature)
        pub.verify(sig, data, ec.ECDSA(hashes.SHA256()))  # type: ignore[arg-type]
        return True
    except InvalidSignature:
        return False
