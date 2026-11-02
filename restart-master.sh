#!/bin/bash
set -euo pipefail

# Master 生产环境重启：使用 docker-compose-prod.yaml 和 .env.production，默认重建 API、Worker 与 Beat。
# 必须用 up --force-recreate 而不是 compose restart：restart 不会重新读取 .env.production。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-prod.yaml}"
ENV_FILE="${ENV_FILE:-.env.production}"
export COMPOSE_FILE ENV_FILE
SERVICES=("$@")

# 读取 .env.production 中的单个键值；文件不存在或键缺失时返回空串。
read_env_value() {
  local key="$1"
  [ -f "${ENV_FILE}" ] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true
}

if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api worker-analysis worker-skill beat)

  # 生产服务名不带 -dev 后缀；凭证齐全时才启动机器人 Worker 与网关。
  bot_id="${CHATBOT_BOT_ID:-$(read_env_value CHATBOT_BOT_ID)}"
  bot_secret="${CHATBOT_BOT_SECRET:-$(read_env_value CHATBOT_BOT_SECRET)}"
  if [ -n "${bot_id}" ] && [ -n "${bot_secret}" ]; then
    SERVICES+=(worker-chatbot chatbot-gateway)
  else
    echo "提示：未配置 CHATBOT_BOT_ID/CHATBOT_BOT_SECRET，跳过 worker-chatbot 与 chatbot-gateway。"
  fi
fi

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate --remove-orphans "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
