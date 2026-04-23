"""
Offline package publisher — generates a self-contained zip for IT-managed deployments.

Output: bootstrap_offline.zip containing:
  - bootstrap_profile.json  (SignedBootstrapProfile wire format)
  - install.cmd             (one command: imports profile then runs installer)
  - README.txt
"""
import io
import json
import zipfile
from pathlib import Path

from bootstrap.schema import SignedBootstrapProfile


def generate_offline_package(
    signed: SignedBootstrapProfile,
    output_path: str,
    installer_name: str = "install.ps1",
) -> Path:
    """
    Write bootstrap_offline.zip to output_path. Returns the Path.
    IT copies the zip + installer to any machine and runs install.cmd — no config needed.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    profile_json = json.dumps(signed.model_dump(), separators=(",", ":"), ensure_ascii=False)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bootstrap_profile.json", profile_json)
        zf.writestr("install.cmd",            _build_install_cmd(installer_name))
        zf.writestr("README.txt",             _README)

    return out


def generate_offline_package_bytes(
    signed: SignedBootstrapProfile,
    installer_name: str = "install.ps1",
) -> bytes:
    """Return the zip as bytes (for HTTP download response)."""
    buf = io.BytesIO()
    profile_json = json.dumps(signed.model_dump(), separators=(",", ":"), ensure_ascii=False)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bootstrap_profile.json", profile_json)
        zf.writestr("install.cmd",            _build_install_cmd(installer_name))
        zf.writestr("README.txt",             _README)
    return buf.getvalue()


def _build_install_cmd(installer_name: str) -> str:
    return (
        "@echo off\r\n"
        "echo WinDiagSvc Bootstrap Installer\r\n"
        "echo ================================\r\n"
        "powershell -ExecutionPolicy Bypass "
        f'-File "{installer_name}" '
        '-BootstrapProfilePath "%~dp0bootstrap_profile.json"\r\n'
        "if %ERRORLEVEL% NEQ 0 pause\r\n"
    )


_README = """\
WinDiagSvc Bootstrap Package — Offline Deployment
==================================================

1. Copy this folder to the target machine (USB, file share, email, etc.)
2. Run install.cmd as Administrator — that is the only step.

The agent reads bootstrap_profile.json for server address and trust settings.
No manual configuration required.

Security: the profile is cryptographically signed by the server's CA key.
          The agent will reject any profile that fails signature verification.
"""
