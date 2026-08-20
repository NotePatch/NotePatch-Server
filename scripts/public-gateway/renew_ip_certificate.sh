#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE=${NOTEPATCH_PUBLIC_GATEWAY_CONFIG:-/etc/notepatch/public-gateway.env}
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
CERT_RENEW_BEFORE_SECONDS=${CERT_RENEW_BEFORE_SECONDS:-259200}
CERT_FILE="/etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem"

exec 9>/run/lock/notepatch-cert-renew.lock
if ! flock -n 9; then
  echo "Another NotePatch certificate renewal is already running."
  exit 0
fi

if [[ ! -r ${CERT_FILE} ]]; then
  echo "Certificate ${CERT_FILE} does not exist." >&2
  exit 1
fi

if openssl x509 -checkend "${CERT_RENEW_BEFORE_SECONDS}" -noout -in "${CERT_FILE}"; then
  echo "Certificate is valid for more than ${CERT_RENEW_BEFORE_SECONDS} seconds; renewal skipped."
  exit 0
fi

echo "Certificate expires within ${CERT_RENEW_BEFORE_SECONDS} seconds; renewing now."
/snap/bin/certbot renew \
  --cert-name "${PUBLIC_IP}" \
  --force-renewal \
  --non-interactive

nginx -t
systemctl reload nginx
echo "Certificate renewed and Nginx reloaded successfully."
