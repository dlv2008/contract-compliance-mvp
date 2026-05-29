#!/usr/bin/env bash
set -euo pipefail

STACK_DIR="/opt/stacks/compliance-app"
COMPOSE_FILE="$STACK_DIR/docker-compose.prod.yml"

if [[ -z "${IMAGE_TAG:-}" ]]; then
  echo "IMAGE_TAG is required"
  exit 1
fi

if [[ -z "${GHCR_PULL_TOKEN:-}" ]]; then
  echo "GHCR_PULL_TOKEN is required"
  exit 1
fi

echo "$GHCR_PULL_TOKEN" | docker login ghcr.io -u dlv2008 --password-stdin
cd "$STACK_DIR"
docker compose -f "$COMPOSE_FILE" pull
docker compose -f "$COMPOSE_FILE" up -d
