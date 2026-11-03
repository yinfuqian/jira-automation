#!/bin/bash
set -euo pipefail

# 按 DEPLOY_ENV 选择环境：dev 使用 docker-compose-dev.yaml 与 -dev 服务名，
# prod 使用 docker-compose-prod.yaml 与不带后缀的服务名。COMPOSE_FILE、ENV_FILE 仍可单独覆盖。
DEPLOY_ENV="${DEPLOY_ENV:-dev}"
case "${DEPLOY_ENV}" in
  dev)
    DEFAULT_COMPOSE_FILE="docker-compose-dev.yaml"
    DEFAULT_ENV_FILE=".env"
    SUFFIX="-dev"
    ;;
  prod|production)
    DEFAULT_COMPOSE_FILE="docker-compose-prod.yaml"
    DEFAULT_ENV_FILE=".env.production"
    SUFFIX=""
    ;;
  *)
    echo "不支持的 DEPLOY_ENV=${DEPLOY_ENV}，可选值：dev、prod" >&2
    exit 1
    ;;
esac

COMPOSE_FILE="${COMPOSE_FILE:-${DEFAULT_COMPOSE_FILE}}"
ENV_FILE="${ENV_FILE:-${DEFAULT_ENV_FILE}}"
export COMPOSE_FILE ENV_FILE

# 基础服务与机器人服务使用同一套名称，按环境追加对应后缀。
# 机器人是可选能力：只有 BotID/Secret 都配置后才启动专用 Worker 与网关，
# 否则网关会因缺少凭证反复重启。
read_env_value() {
  local key="$1"
  [ -f "${ENV_FILE}" ] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true
}

SERVICES=()
for name in api worker-analysis worker-skill beat; do
  SERVICES+=("${name}${SUFFIX}")
done

bot_id="${CHATBOT_BOT_ID:-$(read_env_value CHATBOT_BOT_ID)}"
bot_secret="${CHATBOT_BOT_SECRET:-$(read_env_value CHATBOT_BOT_SECRET)}"
if [ -n "${bot_id}" ] && [ -n "${bot_secret}" ]; then
  for name in worker-chatbot chatbot-gateway; do
    SERVICES+=("${name}${SUFFIX}")
  done
  SERVICES+=("worker-knowledge${SUFFIX}")
else
  echo "提示：未配置 CHATBOT_BOT_ID/CHATBOT_BOT_SECRET，跳过机器人服务。"
fi

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate --remove-orphans "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
