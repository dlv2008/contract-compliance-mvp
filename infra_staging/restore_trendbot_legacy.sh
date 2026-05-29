#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
RESTORE_LOG="$LOG_DIR/restore_legacy_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$RESTORE_LOG") 2>&1

echo "=== restore start $(date '+%F %T %Z') ==="

restore_nginx() {
  echo "=== restoring nginx sites ==="

  rm -f /etc/nginx/sites-enabled/*

  ln -sfn ../sites-available/gen.conf /etc/nginx/sites-enabled/gen.conf
  ln -sfn ../sites-available/ce.conf /etc/nginx/sites-enabled/ce.conf
  ln -sfn /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
  ln -sfn ../sites-available/chome.conf /etc/nginx/sites-enabled/chome.conf
  ln -sfn ../sites-available/matrix.conf /etc/nginx/sites-enabled/matrix.conf
  ln -sfn ../sites-available/git.conf /etc/nginx/sites-enabled/git.conf
  ln -sfn ../sites-available/code.conf /etc/nginx/sites-enabled/code.conf
  ln -sfn ../sites-available/uis.conf /etc/nginx/sites-enabled/uis.conf
  tar -xOf /home/dlv/backup_260528/nginx_sites_enabled.tar.gz sites-enabled/meet.conf > /etc/nginx/sites-enabled/meet.conf
  chown root:root /etc/nginx/sites-enabled/meet.conf
  chmod 644 /etc/nginx/sites-enabled/meet.conf
  rm -f /etc/nginx/sites-enabled/trendbot-maintenance.conf

  nginx -t
  systemctl reload nginx
}

restore_ufw() {
  echo "=== restoring ufw rules ==="
  local allow_rules=(
    "42169/tcp"
    "7856"
    "9480"
    "9443"
    "10000"
    "7443/tcp"
    "7882/udp"
    "50000:60000/udp"
    "7880/tcp"
    "7881/tcp"
    "53/udp"
    "10000/udp"
  )

  for rule in "${allow_rules[@]}"; do
    ufw allow "$rule" || true
  done
}

restore_site_total() {
  echo "=== restoring site_total ==="
  systemctl enable site_total.service
  systemctl restart site_total.service
}

restore_pm2() {
  echo "=== restoring pm2 nanning demo ==="
  if [[ -f /home/dlv/.pm2/dump.pm2.bak ]]; then
    cp -f /home/dlv/.pm2/dump.pm2.bak /home/dlv/.pm2/dump.pm2
    chown dlv:dlv /home/dlv/.pm2/dump.pm2
  fi
  systemctl enable pm2-dlv.service
  systemctl restart pm2-dlv.service
}

restore_sudoers_for_intrascribe() {
  echo "=== restoring intrascribe sudoers for dlv ==="
  cat > /etc/sudoers.d/intrascribe <<'EOF'
# Intrascribe Service Sudoers Configuration
dlv ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/nginx/sites-available/ameeting.conf
dlv ALL=(ALL) NOPASSWD: /bin/ln -sf /etc/nginx/sites-available/ameeting.conf /etc/nginx/sites-enabled/ameeting.conf
dlv ALL=(ALL) NOPASSWD: /usr/sbin/nginx -t
dlv ALL=(ALL) NOPASSWD: /bin/systemctl reload nginx
dlv ALL=(ALL) NOPASSWD: /bin/rm -f /etc/nginx/sites-enabled/ameeting.conf
dlv ALL=(ALL) NOPASSWD: /bin/systemctl start redis-server
dlv ALL=(ALL) NOPASSWD: /bin/systemctl start redis
dlv ALL=(ALL) NOPASSWD: /bin/systemctl stop redis-server
dlv ALL=(ALL) NOPASSWD: /bin/systemctl stop redis
dlv ALL=(ALL) NOPASSWD: /usr/bin/supabase start -x edge-runtime
dlv ALL=(ALL) NOPASSWD: /usr/bin/supabase stop
EOF
  chmod 0440 /etc/sudoers.d/intrascribe
  visudo -c -f /etc/sudoers.d/intrascribe
}

restore_docker() {
  echo "=== restoring docker containers ==="
  local all_ids
  all_ids="$(docker ps -aq)"
  if [[ -n "$all_ids" ]]; then
    docker start $all_ids || true
  fi
}

start_ameeting_processes() {
  echo "=== restoring ameeting processes ==="

  sudo -u dlv bash -lc '
    set -e
    LOG_DIR=/home/dlv/ameeting/logs
    mkdir -p "$LOG_DIR"

    if ! ss -tulpn | grep -q ":3000 "; then
      cd /home/dlv/ameeting/web
      nohup npm run start >"$LOG_DIR/web.log" 2>&1 &
    fi

    if ! ss -tulpn | grep -q ":8002 "; then
      cd /home/dlv/ameeting/backend/diarization_service
      nohup ./.venv/bin/python3 main.py >"$LOG_DIR/diarization_service.log" 2>&1 &
    fi

    if ! ss -tulpn | grep -q ":8001 "; then
      cd /home/dlv/ameeting/backend/stt_service
      nohup ./.venv/bin/python3 main.py >"$LOG_DIR/stt_service.log" 2>&1 &
    fi

    if ! pgrep -af "agent.py dev" >/dev/null; then
      cd /home/dlv/ameeting/backend/agent_service/transcribe_agent
      nohup ./.venv/bin/python3 agent.py dev >"$LOG_DIR/agent.log" 2>&1 &
    fi

    if ! ss -tulpn | grep -q ":8000 "; then
      cd /home/dlv/ameeting/backend/api_service
      nohup ./.venv/bin/python3 main.py >"$LOG_DIR/api_service.log" 2>&1 &
    fi

    if ! pgrep -af "livekit-server --dev" >/dev/null; then
      cd /home/dlv/ameeting
      nohup livekit-server --dev --bind 0.0.0.0 --node-ip 49.233.155.21 --keys "devkey: secret" >"$LOG_DIR/livekit.log" 2>&1 &
    fi
  '
}

verify_restore() {
  echo "=== waiting for restored services ==="
  sleep 10
  echo "--- listeners ---"
  ss -tulpn | grep -E '(:3000|:8000|:8001|:8002|:8016|:54321|:54323|:4096|:5432|:7880|:7881|:7443|:9443|:9480)' || true
  echo "--- nginx headers ---"
  curl -ksI --resolve www.trendbot.cn:443:127.0.0.1 https://www.trendbot.cn/ | sed -n '1,8p'
  echo "--- nanning ---"
  curl -ksI --resolve www.trendbot.cn:443:127.0.0.1 https://www.trendbot.cn/nanning_agents_mvp/ | sed -n '1,8p'
  echo "--- jitsi ---"
  curl -ksI --resolve www.trendbot.cn:9443:127.0.0.1 https://www.trendbot.cn:9443/ | sed -n '1,8p' || true
  echo "--- supabase ---"
  curl -s http://127.0.0.1:54321/health || true
}

restore_nginx
restore_ufw
restore_site_total
restore_pm2
restore_sudoers_for_intrascribe
restore_docker
start_ameeting_processes
verify_restore

echo "=== restore complete $(date '+%F %T %Z') ==="
