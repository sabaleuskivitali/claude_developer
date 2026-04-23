"""
Detect internet connectivity and airgap status.
"""
import socket


def probe_connectivity(timeout: float = 3.0) -> dict:
    """
    Returns {"has_internet": bool, "is_airgapped": bool}.
    is_airgapped = no default route / no internet and no external DNS.
    """
    has_internet = _can_reach_internet(timeout)
    return {
        "has_internet": has_internet,
        "is_airgapped": not has_internet and not _has_default_route(),
    }


def _can_reach_internet(timeout: float) -> bool:
    try:
        sock = socket.create_connection(("1.1.1.1", 53), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def _has_default_route() -> bool:
    """Check if a default route exists (even if internet unreachable)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        sock.close()
        return True
    except OSError:
        return False
