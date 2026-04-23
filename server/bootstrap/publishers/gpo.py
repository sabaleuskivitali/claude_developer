"""
GPO / Intune publisher — generates a deployment package for domain environments.

Output: gpo_package/ directory containing:
  - bootstrap_profile.json  (SignedBootstrapProfile wire format)
  - bootstrap.reg           (registry .reg file for HKLM path)
  - install.cmd             (runs MSI with registry-injected profile)
  - README.txt
"""
import json
import os
from pathlib import Path

from bootstrap.schema import SignedBootstrapProfile

_REGISTRY_KEY = r"HKEY_LOCAL_MACHINE\SOFTWARE\WinDiagSvc\Bootstrap"


def generate_gpo_package(
    signed: SignedBootstrapProfile,
    output_dir: str,
    msi_path: str = "WinDiagSvc.msi",
) -> Path:
    """
    Write GPO package to output_dir. Returns path to the directory.
    msi_path: path to the MSI file to include in install.cmd (relative or absolute).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    profile_json = json.dumps(signed.model_dump(), separators=(",", ":"))

    # bootstrap_profile.json — raw wire format
    (out / "bootstrap_profile.json").write_text(profile_json, encoding="utf-8")

    # bootstrap.reg — imports into HKLM
    reg_content = _build_reg_file(profile_json)
    (out / "bootstrap.reg").write_text(reg_content, encoding="utf-16")

    # install.cmd — import registry then run installer
    (out / "install.cmd").write_text(_build_install_cmd(msi_path), encoding="utf-8")

    # README.txt
    (out / "README.txt").write_text(_README, encoding="utf-8")

    return out


def _build_reg_file(profile_json: str) -> str:
    # Escape backslashes and quotes for .reg format
    escaped = profile_json.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "Windows Registry Editor Version 5.00\r\n\r\n"
        f'[{_REGISTRY_KEY}]\r\n'
        f'"ProfileJson"="{escaped}"\r\n'
    )


def _build_install_cmd(msi_path: str) -> str:
    return (
        "@echo off\r\n"
        "echo Importing bootstrap profile...\r\n"
        'regedit /s bootstrap.reg\r\n'
        "echo Installing agent...\r\n"
        f'msiexec /i "{msi_path}" /qn /l*v install.log\r\n'
        "echo Done.\r\n"
    )


_README = """\
WinDiagSvc Bootstrap Package — GPO Deployment
==============================================

Steps:
1. Copy this folder to a network share accessible from target machines.
2. Create a GPO that:
   a. Runs install.cmd as Computer Startup Script (elevated).
   b. Or: imports bootstrap.reg via GPO Registry Preferences,
      then deploys WinDiagSvc.msi via Software Installation policy.

The agent reads the bootstrap profile from:
  HKLM\\SOFTWARE\\WinDiagSvc\\Bootstrap\\ProfileJson

No manual configuration required on client machines.
"""
