#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
FRP_VERSION=${FRP_VERSION:-0.71.0}
FRP_ARCH=${FRP_ARCH:-linux_amd64}
FRP_SHA256=${FRP_SHA256:-84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716}
CONFIG_FILE=${NOTEPATCH_PUBLIC_GATEWAY_CONFIG:-/etc/notepatch/public-gateway.env}

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${CONFIG_FILE}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
FRPS_PORT=${FRPS_PORT:-7000}
SOURCE_TOKEN_FILE=${FRP_SOURCE_TOKEN_FILE:-}

archive="frp_${FRP_VERSION}_${FRP_ARCH}.tar.gz"
url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${archive}"
tmp_dir=$(mktemp -d)
trap 'rm -rf "${tmp_dir}"' EXIT
curl --fail --location --retry 3 --output "${tmp_dir}/${archive}" "${url}"
echo "${FRP_SHA256}  ${tmp_dir}/${archive}" | sha256sum --check --strict
tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}"
install -m 0755 "${tmp_dir}/frp_${FRP_VERSION}_${FRP_ARCH}/frpc" /usr/local/bin/frpc

install -d -m 0700 /etc/frp
if [[ -n ${SOURCE_TOKEN_FILE} ]]; then
  install -m 0600 "${SOURCE_TOKEN_FILE}" /etc/frp/client_token
elif [[ ! -s /etc/frp/client_token ]]; then
  openssl rand -hex 32 > /etc/frp/client_token
fi
chmod 0600 /etc/frp/client_token
sed -e "s/serverAddr = \"8.137.78.255\"/serverAddr = \"${PUBLIC_IP}\"/" \
    -e "s/serverPort = 7000/serverPort = ${FRPS_PORT}/" \
  "${REPO_ROOT}/infra/frp/frpc.toml.example" > /etc/frp/frpc.toml
chmod 0600 /etc/frp/frpc.toml
install -m 0644 "${REPO_ROOT}/infra/systemd/frpc.service" /etc/systemd/system/frpc.service
systemctl daemon-reload
/usr/local/bin/frpc verify -c /etc/frp/frpc.toml
"${REPO_ROOT}/scripts/public-gateway/install_frp_direct_route.sh"
systemctl disable frpc.service >/dev/null 2>&1 || true
echo "frpc is installed but intentionally not started. Run: systemctl enable --now frpc"
