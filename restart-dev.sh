#!/bin/bash
set -euo pipefail

# 开发环境重启：使用 docker-compose-dev.yaml 和 .env，默认重建 API、两个 Worker 与 Beat。
# 必须用 up --force-recreate 而不是 compose restart：restart 不会重新读取 .env。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-dev.yaml}"
ENV_FILE="${ENV_FILE:-.env}"
export COMPOSE_FILE ENV_FILE
SERVICES=("$@")

read_env_value() {
  local key="$1"
  [ -f "${ENV_FILE}" ] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true
}

if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api-dev worker-analysis-dev worker-skill-dev beat-dev)
  bot_id="${CHATBOT_BOT_ID:-$(read_env_value CHATBOT_BOT_ID)}"
  bot_secret="${CHATBOT_BOT_SECRET:-$(read_env_value CHATBOT_BOT_SECRET)}"
  if [ -n "${bot_id}" ] && [ -n "${bot_secret}" ]; then
    SERVICES+=(worker-chatbot-dev worker-knowledge-dev chatbot-gateway-dev)
  fi
fi

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate --remove-orphans "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
