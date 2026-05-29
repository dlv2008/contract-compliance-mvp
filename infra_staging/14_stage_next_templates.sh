#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

cat > /opt/stacks/compliance-app/docker-compose.prod.yml <<'EOF'
services:
  compliance-api:
    image: ghcr.io/dlv2008/contract-compliance-mvp-api:${IMAGE_TAG:-latest}
    container_name: compliance-api
    restart: unless-stopped
    env_file:
      - /opt/stacks/shared/env/compliance-app.env
    ports:
      - "127.0.0.1:19080:8000"
EOF

cat > /opt/stacks/shared/env/compliance-app.env.example <<'EOF'
APP_NAME=contract-compliance-mvp
APP_ENV=production
MODEL_PROVIDER=minimax
MODEL_BASE_URL=http://replace-me
MODEL_API_KEY=replace-me
RAGFLOW_BASE_URL=http://replace-me
RAGFLOW_API_KEY=replace-me
EOF

cat > /opt/stacks/shared/nginx/compliance.trendbot.cn.conf <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name compliance.trendbot.cn;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name compliance.trendbot.cn;

    ssl_certificate /etc/nginx/certs/trendbot.cer;
    ssl_certificate_key /etc/nginx/certs/trendbot.key;

    location / {
        proxy_pass http://127.0.0.1:19080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 120s;
    }
}
EOF

cat > /opt/stacks/shared/nginx/rag.trendbot.cn.conf <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name rag.trendbot.cn;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name rag.trendbot.cn;

    ssl_certificate /etc/nginx/certs/trendbot.cer;
    ssl_certificate_key /etc/nginx/certs/trendbot.key;

    auth_basic "Restricted";
    auth_basic_user_file /etc/nginx/.htpasswd-ragflow;

    location / {
        proxy_pass http://127.0.0.1:19380;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
EOF

chown -R dlv:dlv /opt/stacks/compliance-app /opt/stacks/shared

{
  echo "[$(date '+%F %T %Z')] next_templates_staged"
  ls -R /opt/stacks/compliance-app /opt/stacks/shared
} >> "$LOG_FILE"

ls -R /opt/stacks/compliance-app /opt/stacks/shared
