#!/bin/bash
set -euo pipefail

# 统一启动入口。
# dev（默认）使用 docker-compose-dev.yaml + .env；
# prod 使用 docker-compose-prod.yaml + .env.production，服务名不带后缀。
# 也可以显式传入服务名，例如：bash start.sh api
DEPLOY_ENV="${DEPLOY_ENV:-dev}"
case "${DEPLOY_ENV}" in
  dev)
    DEFAULT_COMPOSE_FILE="docker-compose-dev.yaml"
    DEFAULT_ENV_FILE=".env"
    SUFFIX=""
    ;;
  prod|production)
    DEPLOY_ENV="prod"
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

if [ ! -f "${COMPOSE_FILE}" ]; then
  echo "找不到 Compose 文件：${COMPOSE_FILE}" >&2
  exit 1
fi
if [ ! -f "${ENV_FILE}" ]; then
  echo "找不到环境文件：${ENV_FILE}" >&2
  exit 1
fi

read_env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true
}

env_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_service() {
  local name="$1"
  if [ "${name}" = "migrate" ]; then
    printf '%s' "migrate"
    return
  fi
  if [ -n "${SUFFIX}" ]; then
    case "${name}" in
      *"${SUFFIX}") printf '%s' "${name}" ;;
      *) printf '%s' "${name}${SUFFIX}" ;;
    esac
  else
    printf '%s' "${name}"
  fi
}

resolve_services() {
  local args=("$@")
  if [ "${#args[@]}" -gt 0 ]; then
    local item
    for item in "${args[@]}"; do
      SERVICES+=("$(normalize_service "${item}")")
    done
    return
  fi

  local name
  for name in api worker-analysis worker-skill beat; do
    SERVICES+=("${name}${SUFFIX}")
  done

  if [ "${DEPLOY_ENV}" = "dev" ]; then
    local bot_id="${CHATBOT_BOT_ID:-$(read_env_value CHATBOT_BOT_ID)}"
    local bot_secret="${CHATBOT_BOT_SECRET:-$(read_env_value CHATBOT_BOT_SECRET)}"
    if [ -n "${bot_id}" ] && [ -n "${bot_secret}" ]; then
      SERVICES+=("worker-chatbot${SUFFIX}" "chatbot-gateway${SUFFIX}")
    else
      echo "提示：未配置 CHATBOT_BOT_ID/CHATBOT_BOT_SECRET，跳过机器人网关与 worker-chatbot。"
    fi

    local knowledge_id="${CHATBOT_KNOWLEDGE_ID:-$(read_env_value CHATBOT_KNOWLEDGE_ID)}"
    local knowledge_secret="${CHATBOT_KNOWLEDGE_SECRET:-$(read_env_value CHATBOT_KNOWLEDGE_SECRET)}"
    if [ -n "${knowledge_id}" ] && [ -n "${knowledge_secret}" ]; then
      SERVICES+=("worker-chatbot-knowledge" "chatbot-gateway-knowledge")
    else
      echo "提示：未配置 CHATBOT_KNOWLEDGE_ID/CHATBOT_KNOWLEDGE_SECRET，跳过知识机器人网关与 worker。"
    fi
  fi

}

echo "启动环境：${DEPLOY_ENV}；Compose：${COMPOSE_FILE}；环境文件：${ENV_FILE}"

if env_truthy "${RUN_MIGRATIONS:-}" && [ "$#" -eq 0 ]; then
  echo "执行数据库迁移（RUN_MIGRATIONS=${RUN_MIGRATIONS}）..."
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" run --rm migrate
fi

SERVICES=()
resolve_services "$@"

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --remove-orphans "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
