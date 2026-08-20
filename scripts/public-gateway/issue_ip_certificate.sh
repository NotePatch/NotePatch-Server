#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CONFIG_FILE=${NOTEPATCH_PUBLIC_GATEWAY_CONFIG:-/etc/notepatch/public-gateway.env}
MODE=${1:-production}

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${CONFIG_FILE}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"

common=(
  certonly
  --webroot
  --webroot-path /var/www/letsencrypt
  --ip-address "${PUBLIC_IP}"
  --preferred-profile shortlived
  --register-unsafely-without-email
  --agree-tos
  --non-interactive
)
case "${MODE}" in
  staging)
    /snap/bin/certbot "${common[@]}" --staging --cert-name "${PUBLIC_IP}-staging"
    echo "Staging certificate succeeded. Run '$0 production' next."
    ;;
  production)
    /snap/bin/certbot "${common[@]}" --cert-name "${PUBLIC_IP}"
    "${REPO_ROOT}/scripts/public-gateway/configure_nginx.sh" tls
    systemctl enable --now notepatch-cert-renew.timer
    systemctl start notepatch-cert-renew.service
    ;;
  *)
    echo "Usage: $0 [staging|production]" >&2
    exit 2
    ;;
esac
