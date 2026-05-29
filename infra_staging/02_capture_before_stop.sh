#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/home/dlv/backup_260528"
STAMP="$(date '+%F %T %Z')"

{
  echo "=== pre-stop snapshot @ $STAMP ==="
  echo
  echo "=== listeners ==="
  ss -tulpn || true
  echo
  echo "=== docker ps ==="
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' || true
  echo
  echo "=== docker inspect summary ==="
  docker ps -q | xargs -r docker inspect --format '{{.Name}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}' || true
  echo
  echo "=== selected processes ==="
  ps -eo pid,user,cmd --sort=pid | grep -E 'ameeting|nanning_agents_mvp|livekit|/app/main|site_total' | grep -v grep || true
  echo
  echo "=== disk ==="
  df -h /
  echo
  echo "=== docker system df ==="
  docker system df || true
} | tee "$LOG_DIR/pre_stop_snapshot.txt"
