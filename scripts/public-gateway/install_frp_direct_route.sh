#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

install -m 0755 \
  "${REPO_ROOT}/scripts/public-gateway/frp_direct_route.sh" \
  /usr/local/sbin/notepatch-frp-direct-route
install -m 0644 \
  "${REPO_ROOT}/infra/systemd/notepatch-frp-direct-route.service" \
  /etc/systemd/system/notepatch-frp-direct-route.service
install -d -m 0755 /etc/systemd/system/frpc.service.d
install -m 0644 \
  "${REPO_ROOT}/infra/systemd/frpc-direct-route.conf" \
  /etc/systemd/system/frpc.service.d/direct-route.conf

systemctl daemon-reload
systemctl enable --now notepatch-frp-direct-route.service
if systemctl is-active --quiet frpc.service; then
  systemctl restart frpc.service
fi

echo "Installed FRP direct-route policy."
ip -4 rule show
ip -4 route get 8.137.78.255
