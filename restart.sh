#!/bin/bash
set -euo pipefail

# 统一重启入口：强制重建容器，确保重新读取环境文件。
# dev（默认）：bash restart.sh
# prod：DEPLOY_ENV=prod bash restart.sh
# 指定服务：bash restart.sh api-dev  或  bash restart.sh api
# 需要先跑迁移：RUN_MIGRATIONS=1 bash restart.sh
DEPLOY_ENV="${DEPLOY_ENV:-dev}"
case "${DEPLOY_ENV}" in
  dev)
    DEFAULT_COMPOSE_FILE="docker-compose-dev.yaml"
    DEFAULT_ENV_FILE=".env"
    SUFFIX="-dev"
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

  local bot_id="${CHATBOT_BOT_ID:-$(read_env_value CHATBOT_BOT_ID)}"
  local bot_secret="${CHATBOT_BOT_SECRET:-$(read_env_value CHATBOT_BOT_SECRET)}"
  if [ -n "${bot_id}" ] && [ -n "${bot_secret}" ]; then
    SERVICES+=("worker-chatbot${SUFFIX}" "chatbot-gateway${SUFFIX}")
  else
    echo "提示：未配置 CHATBOT_BOT_ID/CHATBOT_BOT_SECRET，跳过机器人网关与 worker-chatbot。"
  fi

  local knowledge_enabled="${KNOWLEDGE_ENABLED:-$(read_env_value KNOWLEDGE_ENABLED)}"
  if env_truthy "${knowledge_enabled}" || [ -n "${bot_id}${bot_secret}" ]; then
    SERVICES+=("worker-knowledge${SUFFIX}")
  fi
}

echo "重启环境：${DEPLOY_ENV}；Compose：${COMPOSE_FILE}；环境文件：${ENV_FILE}"

if env_truthy "${RUN_MIGRATIONS:-}" && [ "$#" -eq 0 ]; then
  echo "执行数据库迁移（RUN_MIGRATIONS=${RUN_MIGRATIONS}）..."
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" run --rm migrate
fi

SERVICES=()
resolve_services "$@"

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate --remove-orphans "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
