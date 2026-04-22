#!/bin/bash
set -e

# Find free port in range
PORT=$(python3 -c "
import socket, os
start = int(os.environ.get('PORT_RANGE_START', 49200))
end   = int(os.environ.get('PORT_RANGE_END',   49300))
for p in range(start, end):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', p))
        s.close()
        print(p)
        break
    except OSError:
        pass
")

if [ -z "$PORT" ]; then
    echo "ERROR: no free port in range ${PORT_RANGE_START:-49200}-${PORT_RANGE_END:-49300}" >&2
    exit 1
fi

export PORT
echo "PORT=$PORT" > /app/runtime/port.env
echo "Using port $PORT"

# Generate self-signed TLS cert if absent
mkdir -p /certs
if [ ! -f /certs/server.crt ]; then
    HOST_IP=$(hostname -I | awk '{print $1}')
    openssl req -x509 -newkey rsa:2048 \
        -keyout /certs/server.key \
        -out    /certs/server.crt \
        -days 3650 -nodes \
        -subj "/CN=windiag-server" \
        -addext "subjectAltName=IP:${HOST_IP},DNS:windiag.local"
    openssl x509 -fingerprint -sha256 -noout -in /certs/server.crt \
        | sed 's/SHA256 Fingerprint=//' \
        > /certs/thumbprint.txt
    echo "TLS cert generated. Thumbprint: $(cat /certs/thumbprint.txt)"
fi

# Publish via avahi (host avahi-daemon picks up files from /etc/avahi/services/)
if [ -d /etc/avahi/services ]; then
    cat > /etc/avahi/services/windiag.service << AVAHI_XML
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">WinDiag on %h</name>
  <service>
    <type>_windiag._tcp</type>
    <port>${PORT}</port>
    <txt-record>version=2</txt-record>
  </service>
</service-group>
AVAHI_XML
    echo "mDNS: published _windiag._tcp on port $PORT"
fi

exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --ssl-keyfile /certs/server.key \
    --ssl-certfile /certs/server.crt \
    --loop uvloop \
    --log-level warning
