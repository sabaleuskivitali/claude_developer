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
    SERVER_NAME="${SERVER_NAME:-server}"
    openssl req -x509 -newkey rsa:2048 \
        -keyout /certs/server.key \
        -out    /certs/server.crt \
        -days 3650 -nodes \
        -subj "/CN=${SERVER_NAME}" \
        -addext "subjectAltName=IP:${HOST_IP},DNS:${SERVER_NAME}.local"
    openssl x509 -fingerprint -sha256 -noout -in /certs/server.crt \
        | sed 's/SHA256 Fingerprint=//' \
        > /certs/thumbprint.txt
    echo "TLS cert generated. Thumbprint: $(cat /certs/thumbprint.txt)"
fi

THUMBPRINT=$(cat /certs/thumbprint.txt)

# Publish via avahi (host avahi-daemon picks up files from /etc/avahi/services/)
if [ -d /etc/avahi/services ]; then
    cat > /etc/avahi/services/windiag.service << AVAHI_XML
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">${SERVER_NAME:-WinDiag} on %h</name>
  <service>
    <type>_windiag._tcp</type>
    <port>${PORT}</port>
    <txt-record>version=2</txt-record>
    <txt-record>thumbprint=${THUMBPRINT}</txt-record>
    <txt-record>discovery=49100</txt-record>
  </service>
</service-group>
AVAHI_XML
    echo "mDNS: published _windiag._tcp on port $PORT thumbprint=$THUMBPRINT"
fi

# Plain HTTP discovery server on fixed port 49100 (no TLS, no auth)
# Agents in other subnets can query http://<server-ip>:49100/discovery
python3 - << PYEOF &
import http.server, json, os, sys

class DiscoveryHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/discovery'):
            data = json.dumps({
                'port':        int(os.environ['PORT']),
                'thumbprint':  open('/certs/thumbprint.txt').read().strip(),
                'version':     2,
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *a): pass

try:
    http.server.HTTPServer(('0.0.0.0', 49100), DiscoveryHandler).serve_forever()
except Exception as e:
    print(f'Discovery server error: {e}', file=sys.stderr)
PYEOF
echo "Discovery: HTTP server started on port 49100"

# UDP beacon: broadcast server presence every 30s for cross-subnet agents
python3 - << PYEOF &
import socket, json, time, os, sys

PORT       = int(os.environ['PORT'])
THUMBPRINT = open('/certs/thumbprint.txt').read().strip()
BEACON_PORT = 49101
payload = json.dumps({'port': PORT, 'thumbprint': THUMBPRINT, 'version': 2}).encode()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
while True:
    try:
        sock.sendto(payload, ('255.255.255.255', BEACON_PORT))
    except Exception as e:
        print(f'UDP beacon error: {e}', file=sys.stderr)
    time.sleep(30)
PYEOF
echo "Discovery: UDP beacon started on port 49101 (broadcast every 30s)"

# Cloud heartbeat: ping every 5 minutes so cabinet shows live status
if [ -n "${API_KEY:-}" ] && [ "${API_KEY}" != "pending" ] && [ -n "${CLOUD_URL:-}" ]; then
    python3 - << 'PYEOF' &
import time, os, urllib.request, ssl, sys

api_key   = os.environ.get('API_KEY', '')
cloud_url = os.environ.get('CLOUD_URL', '').rstrip('/')
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

while True:
    try:
        req = urllib.request.Request(
            f"{cloud_url}/api/server-heartbeat",
            method='POST',
            headers={'X-Api-Key': api_key, 'Content-Length': '0'},
            data=b'',
        )
        urllib.request.urlopen(req, context=ctx, timeout=5)
    except Exception as e:
        print(f'heartbeat error: {e}', file=sys.stderr)
    time.sleep(300)
PYEOF
    echo "Cloud heartbeat: pinging ${CLOUD_URL} every 5m"
fi

exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --ssl-keyfile /certs/server.key \
    --ssl-certfile /certs/server.crt \
    --loop uvloop \
    --log-level warning
