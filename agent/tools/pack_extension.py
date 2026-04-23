"""
pack_extension.py — Pack browser-extension/ into a signed CRX3 file.

Usage:
    python tools/pack_extension.py \
        --key    tools/extension.pem \
        --src    browser-extension \
        --output build/extension.crx \
        --id-out build/extension-id.txt

CRX3 format implemented without external protobuf library.
"""

import argparse
import hashlib
import io
import os
import struct
import zipfile
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "cryptography"])
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding


# ---------------------------------------------------------------------------
# Minimal protobuf encoder (no external library needed)
# ---------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    r = b""
    while n > 127:
        r += bytes([0x80 | (n & 0x7F)])
        n >>= 7
    return r + bytes([n])


def proto_bytes(field: int, data: bytes) -> bytes:
    """Encode a length-delimited (wire type 2) protobuf field."""
    tag = (field << 3) | 2
    return _varint(tag) + _varint(len(data)) + data


# ---------------------------------------------------------------------------
# Extension ID derivation
# ---------------------------------------------------------------------------

def pub_key_to_id(pub_der: bytes) -> str:
    h = hashlib.sha256(pub_der).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in h)


# ---------------------------------------------------------------------------
# CRX3 packer
# ---------------------------------------------------------------------------

def pack_crx3(src_dir: str, key_path: str, output_path: str) -> str:
    """
    Pack extension directory into a CRX3 file.
    Returns the 32-char extension ID.
    """
    # Load private key
    key_bytes = Path(key_path).read_bytes()
    private_key = serialization.load_pem_private_key(key_bytes, password=None)

    # Public key DER
    pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Extension ID (binary: first 16 bytes of SHA256)
    crx_id_bytes = hashlib.sha256(pub_der).digest()[:16]
    ext_id       = pub_key_to_id(pub_der)

    # Build ZIP archive of extension directory
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for fname in sorted(files):
                if fname.startswith("."): continue
                fpath   = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, src_dir)
                zf.write(fpath, arcname)
    zip_bytes = zip_buf.getvalue()

    # SignedData proto:  field 1 = crx_id (bytes)
    signed_header = proto_bytes(1, crx_id_bytes)

    # Data to sign = magic prefix + len(signed_header) LE32 + signed_header + zip
    prefix      = b"CRX3 SignedData\x00"
    data_to_sign = prefix + struct.pack("<I", len(signed_header)) + signed_header + zip_bytes

    # RSA-PKCS1v15-SHA256 signature
    sig = private_key.sign(data_to_sign, padding.PKCS1v15(), hashes.SHA256())

    # AsymmetricKeyProof proto: field 1 = public_key, field 2 = signature
    key_proof = proto_bytes(1, pub_der) + proto_bytes(2, sig)

    # CrxFileHeader proto: field 2 = sha256_with_rsa, field 10000 = signed_header_data
    crx_header = proto_bytes(2, key_proof) + proto_bytes(10000, signed_header)

    # CRX3 binary: magic + version(3) + header_len + header + zip
    crx = (
        b"Cr24"
        + struct.pack("<I", 3)
        + struct.pack("<I", len(crx_header))
        + crx_header
        + zip_bytes
    )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(crx)

    return ext_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import base64, json

    parser = argparse.ArgumentParser()
    parser.add_argument("--key",       required=True,  help="Path to PEM private key")
    parser.add_argument("--src",       required=True,  help="Extension source directory")
    parser.add_argument("--output",    required=True,  help="Output .crx path")
    parser.add_argument("--id-out",    required=False, help="Write extension ID to this file")
    parser.add_argument("--pubkey-out",required=False, help="Write base64 public key to this file")
    parser.add_argument("--patch-manifest", required=False,
                        help="Patch 'key' field into this manifest.json (for unpacked ext same ID)")
    args = parser.parse_args()

    ext_id = pack_crx3(args.src, args.key, args.output)
    print(f"Extension ID : {ext_id}")
    print(f"CRX written  : {args.output}  ({Path(args.output).stat().st_size:,} bytes)")

    # Extract base64 public key (needed for manifest.json "key" field)
    key_bytes   = Path(args.key).read_bytes()
    private_key = serialization.load_pem_private_key(key_bytes, password=None)
    pub_der     = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode("ascii")

    if args.id_out:
        Path(args.id_out).write_text(ext_id + "\n")
        print(f"ID saved     : {args.id_out}")

    if args.pubkey_out:
        Path(args.pubkey_out).write_text(pub_b64 + "\n")
        print(f"Pubkey saved : {args.pubkey_out}")

    if args.patch_manifest:
        mp = Path(args.patch_manifest)
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        manifest["key"] = pub_b64
        mp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Manifest key patched: {args.patch_manifest}")


if __name__ == "__main__":
    main()
