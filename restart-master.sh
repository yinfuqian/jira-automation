#!/bin/bash
set -euo pipefail

# Master 生产环境重启：使用 docker-compose-prod.yaml 和 .env.production，默认只重启 API 与 Celery Worker。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-prod.yaml}"
ENV_FILE="${ENV_FILE:-.env.production}"
export COMPOSE_FILE ENV_FILE
SERVICES=("$@")

if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api worker)
fi

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" restart "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"
