#!/usr/bin/env bash
set -euo pipefail

mode="${1:-dry-run}"
image="${OPENCLAW_SANDBOX_IMAGE:-notepatch-openclaw-sandbox:filetools-v1}"

if [[ "${mode}" != "dry-run" && "${mode}" != "--apply" ]]; then
  echo "Usage: $0 [--apply]" >&2
  exit 2
fi

docker image inspect "${image}" >/dev/null
docker run --rm --network none --read-only \
  --tmpfs /tmp --tmpfs /var/tmp --tmpfs /run \
  --cap-drop ALL --security-opt no-new-privileges \
  "${image}" notepatch-file self-test >/dev/null

active_tasks="$(
  docker compose exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM tasks WHERE status IN ('"'"'queued'"'"','"'"'running'"'"') AND task_type IN ('"'"'generate_image_remark'"'"','"'"'openclaw_agent_run'"'"','"'"'extract_questions'"'"','"'"'build_knowledge_base'"'"','"'"'generate_study_notes'"'"','"'"'generate_flashcards'"'"','"'"'grade_homework'"'"','"'"'highlight_study_notes'"'"','"'"'generate_note_supplement'"'"');"'
)"
sandbox_ids="$(docker ps -aq --filter label=openclaw.sandbox=1)"
gateway_ids="$(docker ps -aq --filter label=notepatch.managed=true --filter label=notepatch.kind=openclaw-gateway)"

echo "Sandbox image: ${image}"
echo "Active AI tasks: ${active_tasks}"
echo "Existing OpenClaw sandboxes: $(wc -w <<<"${sandbox_ids}")"
echo "Managed user gateways: $(wc -w <<<"${gateway_ids}")"

if [[ "${mode}" != "--apply" ]]; then
  echo "Dry run only. Re-run with --apply after reviewing the counts."
  exit 0
fi
if [[ "${active_tasks}" != "0" ]]; then
  echo "Refusing rollout while AI tasks are active." >&2
  exit 1
fi

if [[ -n "${sandbox_ids}" ]]; then
  docker rm -f ${sandbox_ids}
fi
if [[ -n "${gateway_ids}" ]]; then
  docker rm -f ${gateway_ids}
fi

docker compose up -d --build api worker chat-worker ai-worker openclaw-supervisor
echo "OpenClaw file-tools sandbox rollout completed."
