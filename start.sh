#!/bin/bash
set -euo pipefail

# 开发环境启动：使用 docker-compose-dev.yaml 和 .env。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-dev.yaml}"
ENV_FILE="${ENV_FILE:-.env}"
export COMPOSE_FILE ENV_FILE

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d api worker
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps api worker
