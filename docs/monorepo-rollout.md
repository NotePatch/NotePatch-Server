# Monorepo Rollout

## Compatibility Boundary

- Business APIs are exposed only below `/api/v1`.
- `/health` remains unversioned.
- OpenAPI is `/api/v1/openapi.json`; Swagger is `/api/v1/docs`.
- Database table names, IDs and SeaweedFS object keys are unchanged.
- Android and the backend must be released together because old routes intentionally return `404`.

## Before Cutover

```bash
docker compose exec -T postgres \
  pg_dump -U notepatch -d notepatch -Fc > /secure/path/notepatch.dump
python3 scripts/migrate_monorepo_runtime.py
python3 scripts/migrate_monorepo_runtime.py --apply --update-existing
docker compose -f compose.yml config --quiet
docker compose -f compose.yml build api admin-web docserver embedding-service
pytest -q
```

The runtime migration copies and checksums DocTr weights plus each user's
`home/.openclaw`, `workspace` and root runtime configuration. It does not copy
npm/cache/tmp directories, modify sources, delete old paths or overwrite a
different destination file.

## Cutover

```bash
docker compose stop api worker ocr-worker openclaw-supervisor tusd admin-web
python3 scripts/migrate_monorepo_runtime.py --apply
docker compose -f compose.yml --profile ocr up -d
docker compose -f compose.yml exec api alembic upgrade head
```

Verify `/health`, service health, user/document/task counts, object downloads,
chat history, one OpenClaw runtime and the upload -> OCR -> learning -> purge
smoke flow before updating Android's base URL to `http://HOST:8001/api/v1`.

## Rollback

Stop the new API/worker processes and start the old compose file. PostgreSQL and
SeaweedFS volume names are explicitly preserved, and the migration never deletes
the old runtime directories. Keep the database dump, external DocTr directory
and old OpenClaw runtime for at least one release cycle.

This host's cutover backups are stored under `/home/usr/notepatch-data/backups/`:
the PostgreSQL custom-format dump and a tarball of the removed legacy repository
layout (including its compose file). Restore the database only when rolling data
back as well; a code-only rollback can reuse the unchanged named volumes.
