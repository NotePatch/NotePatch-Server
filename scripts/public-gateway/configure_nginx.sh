#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CONFIG_FILE=${NOTEPATCH_PUBLIC_GATEWAY_CONFIG:-/etc/notepatch/public-gateway.env}
MODE=${1:-http}

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ ! -r ${CONFIG_FILE} ]]; then
  echo "Missing ${CONFIG_FILE}." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${CONFIG_FILE}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${PUBLIC_PATH_PREFIX:?PUBLIC_PATH_PREFIX is required}"
if [[ ! ${PUBLIC_PATH_PREFIX} =~ ^/np-[0-9a-f]{32}$ ]]; then
  echo "PUBLIC_PATH_PREFIX must match /np-<32 lowercase hex characters>." >&2
  exit 1
fi

case "${MODE}" in
  http)
    TEMPLATE=${REPO_ROOT}/infra/proxy/notepatch-nginx-http.conf.template
    ;;
  tls)
    TEMPLATE=${REPO_ROOT}/infra/proxy/notepatch-nginx-tls.conf.template
    if [[ ! -r /etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem ]]; then
      echo "Certificate /etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem does not exist." >&2
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 [http|tls]" >&2
    exit 2
    ;;
esac

install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge /etc/nginx/sites-available /etc/nginx/sites-enabled
sed -e "s|__PUBLIC_IP__|${PUBLIC_IP}|g" \
    -e "s|__PUBLIC_PATH_PREFIX__|${PUBLIC_PATH_PREFIX}|g" \
    "${TEMPLATE}" > /etc/nginx/sites-available/notepatch-public
rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/notepatch-public /etc/nginx/sites-enabled/notepatch-public
nginx -t
systemctl enable --now nginx
systemctl reload nginx
