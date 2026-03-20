#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="tg-cli-bot.service"

if ! systemctl is-enabled --quiet "$SERVICE_NAME"; then
  echo "service not enabled: $SERVICE_NAME"
  exit 1
fi

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "service not active: $SERVICE_NAME"
  systemctl status "$SERVICE_NAME" --no-pager || true
  exit 1
fi

echo "ok: $SERVICE_NAME is active"
