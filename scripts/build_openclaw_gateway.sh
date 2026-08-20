#!/usr/bin/env bash
set -euo pipefail

source_dir="${OPENCLAW_SOURCE_DIR:-/home/usr/openclaw}"
image="${OPENCLAW_USER_GATEWAY_IMAGE:-openclaw-webui-node-docker:local}"

if [[ ! -f "${source_dir}/Dockerfile" ]]; then
  echo "OpenClaw Dockerfile not found: ${source_dir}/Dockerfile" >&2
  exit 1
fi

docker build \
  --build-arg OPENCLAW_INSTALL_DOCKER_CLI=1 \
  --tag "${image}" \
  "${source_dir}"

docker run --rm --entrypoint sh "${image}" -lc "docker --version"
echo "Built sandbox-capable OpenClaw image: ${image}"
