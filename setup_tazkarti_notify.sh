#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/tazkarti/app"
ENV_DIR="/etc/tazkarti"
SERVICE_FILE="/etc/systemd/system/tazkarti-notify.service"
DOMAIN="84-13-141-41.nip.io"

cd "$APP_DIR"

if [[ ! -f "$ENV_DIR/tazkarti.env" ]]; then
  echo "ERROR: $ENV_DIR/tazkarti.env not found; existing Telegram settings were not changed."
  exit 1
fi
if ! grep -q '^TELEGRAM_BOT_TOKEN=' "$ENV_DIR/tazkarti.env" || ! grep -q '^TELEGRAM_CHAT_ID=' "$ENV_DIR/tazkarti.env"; then
  echo "ERROR: Telegram variables are missing in $ENV_DIR/tazkarti.env."
  exit 1
fi

if ! command -v ss >/dev/null 2>&1; then
  sudo dnf install -y iproute
fi
if sudo ss -ltn | grep -Eq ':(80|443|8765)[[:space:]]'; then
  echo "ERROR: One of ports 80, 443, or 8765 is already in use. No listener was changed."
  sudo ss -ltnp | grep -E ':(80|443|8765)[[:space:]]' || true
  exit 2
fi

sudo install -d -m 750 "$ENV_DIR"
if [[ ! -s "$ENV_DIR/notify.env" ]]; then
  TOKEN="$(openssl rand -hex 32)"
  printf 'INGEST_TOKEN=%s\n' "$TOKEN" | sudo tee "$ENV_DIR/notify.env" >/dev/null
  sudo chmod 600 "$ENV_DIR/notify.env"
fi
TOKEN="$(sudo sed -n 's/^INGEST_TOKEN=//p' "$ENV_DIR/notify.env" | head -n 1)"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: Could not create/read the notification access token."
  exit 1
fi

sudo tee "$SERVICE_FILE" >/dev/null <<'UNIT'
[Unit]
Description=Tazkarti Plus notification receiver
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=opc
Group=opc
WorkingDirectory=/opt/tazkarti/app
EnvironmentFile=/etc/tazkarti/tazkarti.env
EnvironmentFile=/etc/tazkarti/notify.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/tazkarti/app/tazkarti_notify_receiver.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now tazkarti-notify

if ! command -v caddy >/dev/null 2>&1; then
  sudo dnf install -y 'dnf-command(copr)' || sudo dnf install -y dnf-plugins-core
  sudo dnf copr enable -y @caddy/caddy
  sudo dnf install -y caddy
fi

if [[ -f /etc/caddy/Caddyfile ]]; then
  sudo cp -n /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-tazkarti-notify
fi
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
\${DOMAIN} {
    reverse_proxy 127.0.0.1:8765
}
CADDY

sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-service=http
  sudo firewall-cmd --permanent --add-service=https
  sudo firewall-cmd --reload
fi
sudo systemctl enable --now caddy
sudo systemctl restart caddy

echo
echo "=== Receiver status ==="
sudo systemctl --no-pager --full status tazkarti-notify | sed -n '1,12p'
echo
echo "=== Caddy status ==="
sudo systemctl --no-pager --full status caddy | sed -n '1,12p'
echo
echo "=== MacroDroid endpoint ==="
echo "https://\${DOMAIN}/notify"
echo
echo "=== COPY THIS TOKEN INTO MACRODROID HEADER; DO NOT SHARE IT ==="
echo "$TOKEN"
echo
echo "Oracle Cloud networking must allow inbound TCP 80 and 443 to this VM."
