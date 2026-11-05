#!/bin/bash
set -euo pipefail

# 统一停止入口。
# bash stop.sh             停止当前环境全部服务（保留容器和数据）
# bash stop.sh api-dev     只停止指定服务（可传多个）
# bash stop.sh --down      删除当前环境容器与网络（保留数据卷和外部数据库）
# 切换环境使用 DEPLOY_ENV=prod，例如：DEPLOY_ENV=prod bash stop.sh
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

echo "停止环境：${DEPLOY_ENV}；Compose：${COMPOSE_FILE}；环境文件：${ENV_FILE}"

if [ "${1:-}" = "--down" ]; then
  shift
  if [ "$#" -gt 0 ]; then
    echo "--down 会删除整个环境的容器，不能同时指定服务名。" >&2
    exit 1
  fi
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" down --remove-orphans
  exit 0
fi

if [ "$#" -gt 0 ]; then
  SERVICES=()
  for item in "$@"; do
    SERVICES+=("$(normalize_service "${item}")")
  done
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" stop "${SERVICES[@]}"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
else
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" stop
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps
fi
