#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
LOG_FILE="$LOG_DIR/operation.log"

install -d -m 755 /opt/stacks
install -d -m 755 /opt/stacks/shared
install -d -m 755 /opt/stacks/shared/env
install -d -m 755 /opt/stacks/shared/nginx
install -d -m 755 /opt/stacks/shared/backups
install -d -m 755 /opt/stacks/ragflow
install -d -m 755 /opt/stacks/compliance-app

cat > /opt/stacks/README.md <<'EOF'
# Trendbot Stack Layout

- `/opt/stacks/ragflow`: shared RAGFlow deployment stack
- `/opt/stacks/compliance-app`: contract compliance MVP deployment stack
- `/opt/stacks/shared/env`: server-local environment files, never commit to Git
- `/opt/stacks/shared/nginx`: generated or staged nginx snippets for future releases
- `/opt/stacks/shared/backups`: lightweight deployment-time backups
EOF

chown -R dlv:dlv /opt/stacks

rm -f "/home/dlv/backup_260528/operation.log"$'\r' || true

{
  echo "[$(date '+%F %T %Z')] stack_directories_prepared"
  ls -R /opt/stacks
} >> "$LOG_FILE"

ls -R /opt/stacks
