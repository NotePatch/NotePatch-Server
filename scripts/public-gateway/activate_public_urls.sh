#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CONFIG_FILE=${NOTEPATCH_PUBLIC_GATEWAY_CONFIG:-${REPO_ROOT}/.public-gateway.env}
ENV_FILE=${NOTEPATCH_ENV_FILE:-${REPO_ROOT}/.env}
# shellcheck disable=SC1090
source "${CONFIG_FILE}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${PUBLIC_PATH_PREFIX:?PUBLIC_PATH_PREFIX is required}"
if [[ ! ${PUBLIC_PATH_PREFIX} =~ ^/np-[0-9a-f]{32}$ ]]; then
  echo "Invalid PUBLIC_PATH_PREFIX." >&2
  exit 1
fi
if ! curl --fail --silent --show-error --max-time 15 \
  "https://${PUBLIC_IP}${PUBLIC_PATH_PREFIX}/health" >/dev/null; then
  echo "The public HTTPS health endpoint is unavailable; refusing to switch client URLs." >&2
  exit 1
fi
if [[ ! -f ${ENV_FILE} ]]; then
  touch "${ENV_FILE}"
fi

upsert() {
  local key=$1 value=$2 escaped
  escaped=$(printf '%s' "${value}" | sed 's/[&|]/\\&/g')
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${escaped}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

upsert PUBLIC_PATH_PREFIX "${PUBLIC_PATH_PREFIX}"
upsert PUBLIC_API_BASE_URL "https://${PUBLIC_IP}${PUBLIC_PATH_PREFIX}"
upsert TUSD_BASE_URL "https://${PUBLIC_IP}${PUBLIC_PATH_PREFIX}/files/"
upsert SEAWEEDFS_PUBLIC_BASE_URL "https://${PUBLIC_IP}"
upsert VITE_API_BASE_URL "${PUBLIC_PATH_PREFIX}/api/v1"
upsert VITE_TUSD_BASE_URL "https://${PUBLIC_IP}${PUBLIC_PATH_PREFIX}/files/"

echo "Updated ${ENV_FILE}. Rebuild with:"
echo "  docker compose up -d --build api worker chat-worker admin-web"
