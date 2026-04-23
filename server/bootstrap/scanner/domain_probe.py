"""
Detect Active Directory domain presence via DNS SRV records.
Runs on the server (Ubuntu) — checks if an AD domain is visible on the network.
"""
import socket


def probe_domain(hint: str | None = None) -> dict:
    """
    Returns {"type": "ad"|"none", "domain": str|None}.
    hint: override domain name to check (e.g. from env DOMAIN_HINT).
    """
    candidates = []
    if hint:
        candidates.append(hint)

    # Derive from server's own FQDN
    try:
        fqdn = socket.getfqdn()
        if "." in fqdn:
            candidates.append(fqdn.split(".", 1)[1])
    except Exception:
        pass

    for domain in candidates:
        if _has_ldap_srv(domain):
            return {"type": "ad", "domain": domain}

    return {"type": "none", "domain": None}


def _has_ldap_srv(domain: str) -> bool:
    try:
        socket.getaddrinfo(f"_ldap._tcp.{domain}", None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return True
    except (socket.gaierror, OSError):
        return False
