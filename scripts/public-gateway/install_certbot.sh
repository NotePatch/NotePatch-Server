#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if ! command -v snap >/dev/null 2>&1; then
  apt-get update
  apt-get install -y snapd
fi
if ! snap list certbot >/dev/null 2>&1; then
  snap install certbot --classic
fi
install -m 0644 "${REPO_ROOT}/infra/systemd/notepatch-cert-renew.service" /etc/systemd/system/notepatch-cert-renew.service
install -m 0644 "${REPO_ROOT}/infra/systemd/notepatch-cert-renew.timer" /etc/systemd/system/notepatch-cert-renew.timer
install -m 0755 "${REPO_ROOT}/scripts/public-gateway/renew_ip_certificate.sh" /usr/local/sbin/notepatch-renew-ip-certificate
systemctl daemon-reload
/snap/bin/certbot --version
