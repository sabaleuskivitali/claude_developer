# Ubuntu Server Setup

## Prerequisites

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# avahi-daemon (mDNS — agents discover server automatically)
sudo apt install avahi-daemon -y
sudo systemctl enable avahi-daemon
sudo systemctl start avahi-daemon

# Allow avahi to publish custom services
sudo mkdir -p /etc/avahi/services
```

## 1. Configure and start

```bash
cd ~/claude_developer/server

cp .env.example .env
# Required: POSTGRES_PASSWORD, API_KEY, MINIO_ACCESS_KEY, MINIO_SECRET_KEY
nano .env

docker compose up -d

# Verify all containers running
docker compose ps
```

After `diag_api` starts, it automatically:
- Finds a free port in 49200–49300
- Generates a self-signed TLS cert (stored in Docker volume `api_certs`)
- Writes `/etc/avahi/services/windiag.service` → avahi-daemon broadcasts the port via mDNS

Check which port was assigned:
```bash
docker compose exec api cat /app/runtime/port.env
# → PORT=49213
```

Check TLS thumbprint (needed for agent config):
```bash
docker compose exec api cat /certs/thumbprint.txt
```

## 2. Firewall

```bash
# Allow agent connections to the dynamic port range
sudo ufw allow from 192.168.0.0/16 to any port 49200:49300 proto tcp

# mDNS (agents discover server — UDP 5353)
sudo ufw allow from 192.168.0.0/16 to any port 5353 proto udp

# SMB (legacy — keep until all agents migrated to HTTP API)
sudo ufw allow from 192.168.0.0/16 to any port 445
sudo ufw allow from 192.168.0.0/16 to any port 139

sudo ufw enable
```

## 3. Verify API

```bash
# Health check (get port first)
PORT=$(docker compose exec api cat /app/runtime/port.env | cut -d= -f2)
curl -k https://localhost:$PORT/health

# Test error endpoint
curl -k -X POST https://localhost:$PORT/api/v1/errors \
  -H "X-Api-Key: $(grep API_KEY .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"machine_id":"test","stage":"manual_test","error":"ok","ts":"2026-04-22T10:00:00Z"}'
```

## 4. OTA update packages

```bash
mkdir -p /home/nubes/updates/v1.0.31
# Copy WinDiagSvc.zip to /home/nubes/updates/v1.0.31/
# Write latest.json:
cat > /home/nubes/updates/latest.json << 'EOF'
{
  "version": "1.0.31",
  "released_at": "2026-04-22T00:00:00Z",
  "package_path": "v1.0.31",
  "sha256": "<sha256 of zip>",
  "min_version": "1.0.0",
  "changelog": "Replaced SMB with HTTP API"
}
EOF
```

## Daily operations

```bash
# Machine status
POSTGRES_DSN="postgresql://diag:PASSWORD@localhost:5432/diag" python manage.py status

# Recent install errors
docker compose exec postgres psql -U diag -d diag \
  -c "SELECT machine_id, stage, error, received_at FROM install_errors ORDER BY received_at DESC LIMIT 20;"

# Tail API logs
docker compose logs -f api

# Restart a machine
PORT=$(docker compose exec api cat /app/runtime/port.env | cut -d= -f2)
curl -k -X POST https://localhost:$PORT/api/v1/...
```

## SMB (legacy, keep until migration complete)

```bash
sudo apt install samba -y
sudo mkdir -p /home/nubes/share
sudo tee -a /etc/samba/smb.conf <<'EOF'
[Share]
   path = /home/nubes/share
   browseable = no
   guest ok = yes
   read only = no
   create mask = 0664
EOF
sudo systemctl restart smbd
```
