"""
DNS-SD / mDNS publisher — advertises the bootstrap server on the local L2 segment.

Service type: _windiag._tcp.local
TXT record includes profile_fingerprint so agents can verify before trusting.

Requires: zeroconf package (added to requirements.txt).
"""
import hashlib
import logging
import threading

from bootstrap.schema import SignedBootstrapProfile

log = logging.getLogger(__name__)

_SERVICE_TYPE = "_windiag._tcp.local."
_SERVICE_NAME = "WinDiag Bootstrap._windiag._tcp.local."

_zc_instance = None
_info_instance = None
_lock = threading.Lock()


def start(signed: SignedBootstrapProfile, port: int) -> None:
    """
    Advertise the bootstrap service via mDNS.
    profile_fingerprint in TXT record = first 16 chars of SHA-256(signed_data).
    Call stop() before calling start() again with a new profile.
    """
    global _zc_instance, _info_instance
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket

        fingerprint = hashlib.sha256(signed.signed_data.encode()).hexdigest()[:16]
        local_ip = _get_local_ip()

        info = ServiceInfo(
            type_=_SERVICE_TYPE,
            name=_SERVICE_NAME,
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={
                b"fp": fingerprint.encode(),     # profile fingerprint for MITM check
                b"v":  b"1",                     # protocol version
            },
            server=f"{socket.gethostname()}.local.",
        )

        with _lock:
            if _zc_instance:
                _zc_instance.close()
            _zc_instance = Zeroconf()
            _zc_instance.register_service(info)
            _info_instance = info
            log.info("DNS-SD: advertising %s on %s:%d fp=%s", _SERVICE_NAME, local_ip, port, fingerprint)

    except ImportError:
        log.warning("zeroconf not installed — DNS-SD publisher disabled")
    except Exception as e:
        log.error("DNS-SD start failed: %s", e)


def stop() -> None:
    global _zc_instance, _info_instance
    with _lock:
        if _zc_instance and _info_instance:
            try:
                _zc_instance.unregister_service(_info_instance)
                _zc_instance.close()
            except Exception:
                pass
        _zc_instance = None
        _info_instance = None


def _get_local_ip() -> str:
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"
