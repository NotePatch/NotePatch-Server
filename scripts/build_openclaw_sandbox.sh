#!/usr/bin/env bash
set -euo pipefail

image="${OPENCLAW_SANDBOX_IMAGE:-notepatch-openclaw-sandbox:filetools-v1}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
candidate="${image}-candidate-$(date +%s)"

cleanup() {
  docker image rm "${candidate}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build \
  --file "${root}/infra/docker/openclaw-sandbox.Dockerfile" \
  --tag "${candidate}" \
  "${root}"

docker run --rm --network none --read-only \
  --tmpfs /tmp --tmpfs /var/tmp --tmpfs /run \
  --cap-drop ALL --security-opt no-new-privileges \
  "${candidate}" notepatch-file self-test
docker image tag "${candidate}" "${image}"
echo "Built and verified ${image}"
