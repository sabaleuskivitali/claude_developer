"""
Detect network topology: L2 reachability (multicast) vs L3-only.
"""
import socket


def probe_network() -> dict:
    """
    Returns {"l2_reachable": bool}.
    L2 reachable = server can emit mDNS multicast (224.0.0.251:5353).
    If the kernel rejects the send, we're behind a router that blocks multicast.
    """
    l2_reachable = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.settimeout(0.5)
        sock.sendto(b"\x00", ("224.0.0.251", 5353))
        l2_reachable = True
        sock.close()
    except OSError:
        pass

    return {"l2_reachable": l2_reachable}
