#!/usr/bin/env bash
# One-shot setup for Relay's cloud mode on a fresh Ubuntu 24.04 Vultr server.
#
# Run as root, from inside the cloned repo:
#     bash deploy/setup.sh 149.28.41.199
#
# It installs Python + Caddy (free HTTPS), copies Relay to /opt/relay, makes a
# systemd service, and opens the firewall. You fill in API keys afterwards.
set -euo pipefail

IP="${1:?Usage: bash deploy/setup.sh <server-ip>}"
HOST="${IP//./-}.sslip.io"      # 149.28.41.199 -> 149-28-41-199.sslip.io (free HTTPS name)
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3-venv python3-pip git ufw curl gpg debian-keyring debian-archive-keyring apt-transport-https

echo "==> Installing Caddy (automatic HTTPS)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> Copying Relay to /opt/relay"
id relay >/dev/null 2>&1 || adduser --system --group --home /opt/relay relay
mkdir -p /opt/relay
cp -a "$SRC/." /opt/relay/
cd /opt/relay
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
# The phone/Mac libraries aren't needed on the server; skip any that fail to build.
grep -v -iE '^(pyobjc|pyautogui|mss)' requirements.txt > /tmp/relay-req.txt
.venv/bin/pip install -r /tmp/relay-req.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "CLOUD_MODE=1" >> .env
fi
grep -q '^CLOUD_MODE=1' .env || echo "CLOUD_MODE=1" >> .env
chown -R relay:relay /opt/relay
chmod 600 /opt/relay/.env

echo "==> Service"
cp /opt/relay/deploy/relay.service /etc/systemd/system/relay.service
systemctl daemon-reload
systemctl enable relay

echo "==> HTTPS (Caddy) for $HOST"
sed "s/1-2-3-4.sslip.io/$HOST/" /opt/relay/deploy/Caddyfile > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> Firewall"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

cat <<EOF

============================================================
Almost done. Two steps left:

1. Add your keys (same values as your Mac's .env, INCLUDING the same RELAY_TOKEN
   so your Shortcut keeps working):
       nano /opt/relay/.env
   Save: Ctrl+O, Enter. Exit: Ctrl+X.

2. Start Relay:
       systemctl restart relay && sleep 2 && curl -s https://$HOST/health

Your server address:  https://$HOST
On the Mac:           python -m relay.device_helper --server https://$HOST
Shortcut URL:         https://$HOST/command
============================================================
EOF
