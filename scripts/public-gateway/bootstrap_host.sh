#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SOURCE_CONFIG=${1:-${REPO_ROOT}/.public-gateway.env}
SOURCE_TOKEN=${FRP_SOURCE_TOKEN_FILE:-${REPO_ROOT}/.frp-client-token}
if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0 [path-to-public-gateway.env]" >&2
  exit 1
fi
if [[ ! -r ${SOURCE_CONFIG} ]]; then
  echo "Missing ${SOURCE_CONFIG}." >&2
  exit 1
fi
if [[ ! -r ${SOURCE_TOKEN} ]]; then
  echo "Missing FRP token file ${SOURCE_TOKEN}." >&2
  exit 1
fi

apt-get update
apt-get install -y nginx curl ca-certificates openssl
install -d -m 0700 /etc/notepatch
install -m 0600 "${SOURCE_CONFIG}" /etc/notepatch/public-gateway.env
"${REPO_ROOT}/scripts/public-gateway/configure_nginx.sh" http
"${REPO_ROOT}/scripts/public-gateway/install_certbot.sh"
FRP_SOURCE_TOKEN_FILE="${SOURCE_TOKEN}" "${REPO_ROOT}/scripts/public-gateway/install_frpc.sh"
echo "Host bootstrap complete. frpc and TLS remain inactive until explicitly enabled."
