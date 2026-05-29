#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"
STAMP="$(date '+%F %T %Z')"

mkdir -p /var/www/maintenance
cp /home/dlv/trendbot_maintenance.html /var/www/maintenance/index.html
cat > /etc/nginx/sites-available/trendbot-maintenance.conf <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name trendbot.cn *.trendbot.cn _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2 default_server;
    listen [::]:443 ssl http2 default_server;
    server_name trendbot.cn *.trendbot.cn _;

    ssl_certificate /etc/nginx/certs/trendbot.cer;
    ssl_certificate_key /etc/nginx/certs/trendbot.key;

    root /var/www/maintenance;
    index index.html;

    location / {
        try_files $uri /index.html;
    }
}
EOF

for entry in /etc/nginx/sites-enabled/*; do
  name="$(basename "$entry")"
  if [[ "$name" != "trendbot-maintenance.conf" ]]; then
    rm -f "$entry"
  fi
done

ln -sfn /etc/nginx/sites-available/trendbot-maintenance.conf /etc/nginx/sites-enabled/trendbot-maintenance.conf

nginx -t
systemctl reload nginx

{
  echo "[$STAMP] nginx_maintenance_cutover complete"
  echo "enabled sites:"
  ls -1 /etc/nginx/sites-enabled
} >> "$LOG_FILE"

curl -kI --resolve www.trendbot.cn:443:127.0.0.1 https://www.trendbot.cn/ | sed -n '1,8p'
echo "---"
curl -kI --resolve meet.trendbot.cn:443:127.0.0.1 https://meet.trendbot.cn/ | sed -n '1,8p'
echo "---"
curl -kI --resolve code.trendbot.cn:443:127.0.0.1 https://code.trendbot.cn/ | sed -n '1,8p'
