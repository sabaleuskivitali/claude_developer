# Ubuntu Server Setup

## Prerequisites

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Samba (SMB share for clients)
sudo apt install samba cifs-utils -y
```

## 1. SMB Share

```bash
# Create share directory
sudo mkdir -p /mnt/diag
sudo chown -R $USER:$USER /mnt/diag

# Add to /etc/samba/smb.conf
sudo tee -a /etc/samba/smb.conf <<'EOF'
[diag]
   path = /mnt/diag
   browseable = no
   read only = no
   valid users = svc_diag
   create mask = 0664
   directory mask = 0775
EOF

# Create SMB user
sudo smbpasswd -a svc_diag

sudo systemctl restart smbd
sudo systemctl enable smbd
```

## 2. Server

```bash
cd ~/claude_developer/server

cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD and ANTHROPIC_API_KEY
nano .env

docker compose up -d

# Verify schema was applied
docker compose exec postgres psql -U diag -d diag -c "\dt"
```

## 3. Verify

```bash
# Check containers
docker compose ps

# Tail ETL logs
docker compose logs -f etl

# Run manage.py (install psycopg2 locally or exec inside container)
pip install psycopg2-binary
POSTGRES_DSN="postgresql://diag:YOUR_PASSWORD@localhost:5432/diag" \
SMB_SHARE_PATH=/mnt/diag \
python manage.py status
```

## 4. Firewall

```bash
# Allow SMB from LAN only
sudo ufw allow from 192.168.0.0/16 to any port 445
sudo ufw allow from 192.168.0.0/16 to any port 139
# Block PostgreSQL from outside (already bound to 127.0.0.1)
sudo ufw enable
```

## Daily operations

```bash
# Machine status
python manage.py status

# Performance overview
python manage.py perf

# Recent errors on a machine
python manage.py logs <machine_id> --errors --tail 30

# Restart offline machines
python manage.py restart --offline

# Force ETL run now
python manage.py etl
```
