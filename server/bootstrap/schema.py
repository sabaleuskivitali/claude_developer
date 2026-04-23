import json
from pydantic import BaseModel, Field
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4


class BootstrapEndpoints(BaseModel):
    primary: str
    secondary: str | None = None
    policy: str | None = None


class BootstrapTrust(BaseModel):
    ca_cert: str = ""  # server TLS cert PEM (base64), empty if self-signed not pinned
    pins: list[str] = []  # sha256// prefixed HPKP-style pins


class BootstrapEnrollment(BaseModel):
    token: str
    csr_endpoint: str
    expires_at: str  # ISO 8601 UTC


class BootstrapProfile(BaseModel):
    profile_id: str = Field(default_factory=lambda: str(uuid4()))
    version: str = "1"
    tenant_id: str
    site_id: str = "default"
    issued_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str
    endpoints: BootstrapEndpoints
    trust: BootstrapTrust = Field(default_factory=BootstrapTrust)
    enrollment: BootstrapEnrollment

    def canonical_bytes(self) -> bytes:
        """Deterministic JSON for signing — all fields sorted recursively."""
        d = self.model_dump(mode="json")
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


class SignedBootstrapProfile(BaseModel):
    """Wire format: signed_data = base64(canonical JSON), signature = base64(ECDSA sig)."""
    signed_data: str  # base64-encoded BootstrapProfile canonical JSON
    signature: str    # base64-encoded ECDSA-P256-SHA256 signature of signed_data bytes

    def get_profile(self) -> BootstrapProfile:
        import base64
        raw = base64.b64decode(self.signed_data).decode()
        return BootstrapProfile.model_validate_json(raw)


class DeploymentContext(BaseModel):
    has_ad: bool = False
    ad_domain: str | None = None
    has_aad: bool = False
    l2_reachable: bool = False
    has_internet: bool = False
    is_airgapped: bool = False
    server_url: str = ""
    tenant_id: str = ""
    site_id: str = "default"
