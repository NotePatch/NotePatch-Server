# Repository Guidelines

## Project Structure & Module Organization

NotePatch is a monorepo. Backend code lives in `backend/src/notepatch/`: `entrypoints/` contains the API, workers, and supervisor; `platform/` contains shared infrastructure; and `modules/` is organized by domain (`identity`, `documents`, `tasks`, `learning`, `ai`, and `admin`). Alembic revisions are in `backend/migrations/`, with backend tests in `backend/tests/`.

Standalone inference services live under `services/doctr/` and `services/embedding/`, each with its own `src/` and `tests/`. The React/Vite admin UI is in `web/admin/`. OpenClaw skills and runtime templates are versioned in `openclaw/`; Docker definitions are in `infra/docker/`. Do not commit runtime data, model weights, uploaded files, or generated per-user OpenClaw directories.

## Build, Test, and Development Commands

- `docker compose up -d --build api worker chat-worker`: build and start the API and CPU workers.
- `docker compose --profile ocr up -d --build ocr-worker`: start the optional GPU OCR worker.
- `docker compose exec api alembic upgrade head`: apply database migrations.
- `docker compose run --rm --no-deps api pytest -q`: run the complete Python test suite in the project image.
- `python3 -m compileall backend/src services scripts`: check Python syntax locally.
- `docker compose config --quiet`: validate Compose configuration.
- `cd web/admin && npm install && npm run build`: type-check and build the admin UI.

## Coding Style & Naming Conventions

Use four spaces, type hints, and `snake_case` for Python functions/modules; use `PascalCase` for classes and Pydantic/ORM models. Keep API, schema, service, and model code inside the owning domain module. React components use `PascalCase`, hooks use `useCamelCase`, and TypeScript uses two-space indentation. Prefer existing service abstractions over direct Redis, boto3, Docker, or database access from routes. Keep all workspace-owned queries scoped by both `workspace_id` and resource ID.

## Testing Guidelines

Pytest discovers `test_*.py` under the three configured test directories. Add focused regression tests for permissions, queue routing, cancellation, and storage side effects. Unit tests must use explicit fakes and must not download OCR/embedding models or call a live OpenClaw gateway. Run targeted tests first, then the full suite and `docker compose config --quiet`.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects, for example `Route OpenClaw chat to dedicated worker`. Keep commits scoped and include migrations with their model changes. Pull requests should explain behavior, migration/deployment impact, tests run, and linked issues. Include screenshots for admin UI changes and sample request/response payloads for API contract changes. Never commit `.env`, credentials, provider keys, or files under `${NOTEPATCH_DATA_ROOT}`.
