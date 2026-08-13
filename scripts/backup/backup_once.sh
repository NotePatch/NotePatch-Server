#!/bin/sh
set -eu
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="/tmp/notepatch-backup-$stamp"
mkdir -p "$staging/postgres" "$staging/seaweedfs" "$staging/config"
trap 'rm -rf "$staging"' EXIT
pg_dump "$DATABASE_URL_PLAIN" --format=custom --file="$staging/postgres/notepatch.dump"
ready=0
for attempt in $(seq 1 60); do
  if AWS_ACCESS_KEY_ID="$SEAWEEDFS_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$SEAWEEDFS_SECRET_KEY" \
    aws --endpoint-url "$SEAWEEDFS_S3_ENDPOINT" s3api head-bucket --bucket "$SEAWEEDFS_BUCKET" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "SeaweedFS S3 did not become ready" >&2
  exit 1
fi
AWS_ACCESS_KEY_ID="$SEAWEEDFS_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$SEAWEEDFS_SECRET_KEY" \
  aws --endpoint-url "$SEAWEEDFS_S3_ENDPOINT" s3 sync "s3://$SEAWEEDFS_BUCKET" "$staging/seaweedfs" --no-progress
printf 'created_at=%s\nschema_revision=%s\nrelease_revision=%s\n' "$stamp" "${SCHEMA_REVISION:-unknown}" "${RELEASE_REVISION:-unknown}" > "$staging/config/manifest.txt"
checksum_file="$(mktemp)"
(
  cd "$staging"
  find . -type f ! -path './config/SHA256SUMS' -print0 \
    | sort -z \
    | xargs -0 sha256sum
) > "$checksum_file"
mv "$checksum_file" "$staging/config/SHA256SUMS"
if [ ! -f "$RESTIC_REPOSITORY/config" ]; then restic init; fi
restic backup "$staging" --tag notepatch-daily --host notepatch-server
restic forget --tag notepatch-daily --keep-daily 14 --prune
restic check
