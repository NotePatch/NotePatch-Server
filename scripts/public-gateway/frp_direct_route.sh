#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE=${NOTEPATCH_PUBLIC_GATEWAY_CONFIG:-/etc/notepatch/public-gateway.env}
if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ -r ${CONFIG_FILE} ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

FRP_SERVER_IP=${FRP_SERVER_IP:-${PUBLIC_IP:-8.137.78.255}}
FRP_DIRECT_RULE_PRIORITY=${FRP_DIRECT_RULE_PRIORITY:-80}
RULE_PATTERN="^[[:space:]]*${FRP_DIRECT_RULE_PRIORITY}:[[:space:]]+from all to ${FRP_SERVER_IP}/32 lookup main$"

case "${1:-add}" in
  add)
    if ! ip -4 rule show | grep -Eq "${RULE_PATTERN}"; then
      ip -4 rule add priority "${FRP_DIRECT_RULE_PRIORITY}" \
        to "${FRP_SERVER_IP}/32" table main
    fi
    ip -4 route flush cache
    ip -4 route get "${FRP_SERVER_IP}"
    ;;
  delete)
    while ip -4 rule show | grep -Eq "${RULE_PATTERN}"; do
      ip -4 rule delete priority "${FRP_DIRECT_RULE_PRIORITY}" \
        to "${FRP_SERVER_IP}/32" table main
    done
    ip -4 route flush cache
    ;;
  *)
    echo "Usage: $0 [add|delete]" >&2
    exit 2
    ;;
esac
