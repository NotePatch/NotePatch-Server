from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path


NOTEPATCH_SKILLS = (
    "notepatch_file_reader",
    "notepatch_question_extractor",
    "notepatch_kb_builder",
    "notepatch_scholar_notes",
    "notepatch_note_supplement",
    "notepatch_grading",
    "notepatch_note_highlighter",
    "notepatch_flashcards",
)
LEGACY_NOTEPATCH_SKILLS = {
    "notepatch-kb-builder",
    "notepatch-scholar-notes",
    "notepatch-grading",
    "notepatch-note-highlighter",
}
RUNTIME_ENV_SECRET_KEYS = {"OPENAI_API_KEY"}


class OpenClawRuntimeConfig:
    settings: object
    asset_root: Path

    def _template(self, name: str) -> str:
        path = self.asset_root / "templates" / name
        if not path.is_file():
            from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError

            raise OpenClawUserRuntimeError(f"OpenClaw template not found: {path}")
        return path.read_text(encoding="utf-8")

    def _ensure_env(self, user_id: str) -> str:
        env_path = self.env_path(user_id)
        existing = self._read_env(env_path)
        for key in RUNTIME_ENV_SECRET_KEYS:
            existing.pop(key, None)
        token = existing.get("OPENCLAW_GATEWAY_TOKEN") or (
            f"{self.settings.openclaw_user_gateway_token_prefix}-{secrets.token_hex(24)}"
        )
        values = {
            "OPENCLAW_CONTAINER_NAME": self.container_name(user_id),
            "OPENCLAW_GATEWAY_TOKEN": token,
            "OPENCLAW_USER_GATEWAY_IMAGE": self.settings.openclaw_user_gateway_image,
            "OPENCLAW_DOCKER_NETWORK": self.settings.openclaw_docker_network,
        }
        docker_socket_gid = self.docker_socket_gid()
        if docker_socket_gid is not None:
            values["OPENCLAW_DOCKER_SOCKET_GID"] = str(docker_socket_gid)
        self._write_text_if_changed(
            env_path,
            "\n".join(f"{key}={value}" for key, value in {**existing, **values}.items()) + "\n",
        )
        return token

    def _ensure_openclaw_json(
        self, user_id: str, *, user, workspace, model_ids: tuple[str, ...] | None = None
    ) -> None:
        models: dict = {}
        base_url = (self.settings.openai_base_url or "").strip()
        model_definitions = self._openai_model_definitions(user, model_ids)
        if base_url:
            models = {
                "providers": {"openai": {"baseUrl": base_url, "models": model_definitions}}
            }
        replacements = {
            "__WORKSPACE_DIR_JSON__": json.dumps(str(self.workspace_dir(user_id))),
            "__MODEL_JSON__": json.dumps(self.settings.openclaw_agent_model),
            "__SKILLS_JSON__": json.dumps(list(NOTEPATCH_SKILLS)),
            "__MODELS_JSON__": json.dumps(models),
            "__SANDBOX_IMAGE_JSON__": json.dumps(self.settings.openclaw_sandbox_image),
            "__SANDBOX_CONTAINER_PREFIX_JSON__": json.dumps(
                self.sandbox_container_prefix(user_id)
            ),
            "__SANDBOX_MEMORY_JSON__": json.dumps(self.settings.openclaw_sandbox_memory_limit),
            "__SANDBOX_CPUS__": json.dumps(self.settings.openclaw_sandbox_cpus),
            "__SANDBOX_PIDS_LIMIT__": str(self.settings.openclaw_sandbox_pids_limit),
            "__SANDBOX_EXEC_TIMEOUT__": str(self.settings.openclaw_sandbox_exec_timeout_seconds),
        }
        rendered = self._template("openclaw.json.template")
        for token, value in replacements.items():
            rendered = rendered.replace(token, value)
        managed = json.loads(rendered)
        config_path = self.openclaw_json_path(user_id)
        payload: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError

                raise OpenClawUserRuntimeError(f"Invalid OpenClaw config: {config_path}") from exc
            if isinstance(existing, dict):
                payload = existing

        payload.pop("identity", None)
        agents = payload.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        defaults.update(managed["agents"]["defaults"])
        payload["tools"] = managed["tools"]
        if models:
            existing_definitions = (
                (((payload.get("models") or {}).get("providers") or {}).get("openai") or {}).get("models")
                or []
            )
            definitions = models["providers"]["openai"]["models"]
            known_ids = {item.get("id") for item in definitions if isinstance(item, dict)}
            for item in existing_definitions:
                if isinstance(item, dict) and item.get("id") not in known_ids:
                    definitions.append(item)
                    known_ids.add(item.get("id"))
            payload["models"] = models
        else:
            providers = (payload.get("models") or {}).get("providers")
            if isinstance(providers, dict):
                openai_provider = providers.get("openai")
                if isinstance(openai_provider, dict):
                    openai_provider.pop("baseUrl", None)
                    if openai_provider.get("models") == []:
                        openai_provider.pop("models", None)
                    if not openai_provider:
                        providers.pop("openai", None)
                if not providers:
                    payload.pop("models", None)
        self._write_json_if_changed(config_path, payload)

    def _openai_model_definitions(
        self, user, model_ids: tuple[str, ...] | None
    ) -> list[dict]:
        configured = [
            self.settings.openclaw_agent_model,
            self.settings.ai_chat_title_model,
            self.settings.ai_image_remark_model,
            getattr(user, "preferred_ai_model", None),
            *(model_ids or ()),
        ]
        definitions: list[dict] = []
        seen: set[str] = set()
        for model_id in configured:
            if not isinstance(model_id, str) or not model_id.startswith("openai/"):
                continue
            upstream_id = model_id.split("/", 1)[1].strip()
            if not upstream_id or upstream_id in seen:
                continue
            seen.add(upstream_id)
            definitions.append(
                {
                    "id": upstream_id,
                    "name": upstream_id,
                    "input": ["text", "image"],
                }
            )
        return definitions


    def _ensure_auth_profiles(self, user_id: str) -> None:
        payload = json.loads(self._template("auth-profiles.json.template"))
        self._write_json_if_changed(self.auth_profiles_path(user_id), payload, mode=0o600)

    def _ensure_notepatch_skills(self, user_id: str) -> None:
        for skill_id in (*LEGACY_NOTEPATCH_SKILLS, *NOTEPATCH_SKILLS):
            shutil.rmtree(self.legacy_skills_dir(user_id) / skill_id, ignore_errors=True)
        for skill_id in LEGACY_NOTEPATCH_SKILLS:
            shutil.rmtree(self.skills_dir(user_id) / skill_id, ignore_errors=True)
        for skill_id in NOTEPATCH_SKILLS:
            source = self.asset_root / "skills" / skill_id / "SKILL.md"
            if not source.is_file():
                from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError

                raise OpenClawUserRuntimeError(f"OpenClaw skill not found: {source}")
            self._write_text_if_changed(
                self.skills_dir(user_id) / skill_id / "SKILL.md",
                source.read_text(encoding="utf-8"),
            )

    def _write_notepatch_runtime(self, user_id: str, *, user, workspace) -> None:
        self._write_json_if_changed(
            self.notepatch_runtime_path(user_id),
            {
                "notepatch_user_id": user.id,
                "notepatch_workspace_id": workspace.id,
                "display_name": user.full_name or user.email,
                "workspace_dir": str(self.workspace_dir(user_id)),
                "container_name": self.container_name(user_id),
                "gateway_url": self.gateway_url(user_id),
            },
        )

    def _write_compose(self, user_id: str) -> None:
        docker_socket_gid = self.docker_socket_gid()
        group_add = ""
        if docker_socket_gid is not None:
            group_add = (
                "    group_add:\n"
                f'      - "${{OPENCLAW_DOCKER_SOCKET_GID:-{docker_socket_gid}}}"'
            )
        replacements = {
            "__HOME_DIR__": str(self.home_dir(user_id)),
            "__CACHE_DIR__": str(self.cache_dir(user_id)),
            "__TMP_DIR__": str(self.tmp_dir(user_id)),
            "__WORKSPACE_DIR__": str(self.workspace_dir(user_id)),
            "__GROUP_ADD_BLOCK__": group_add,
            "__GATEWAY_COMMAND__": self.gateway_command().replace(
                "$OPENCLAW_GATEWAY_TOKEN", "$$OPENCLAW_GATEWAY_TOKEN"
            ),
            "__DEFAULT_IMAGE__": self.settings.openclaw_user_gateway_image,
            "__DEFAULT_CONTAINER_NAME__": self.container_name(user_id),
            "__DEFAULT_NETWORK__": self.settings.openclaw_docker_network,
        }
        rendered = self._template("user-compose.yml.template")
        for token, value in replacements.items():
            rendered = rendered.replace(token, value)
        self._write_text_if_changed(self.compose_path(user_id), rendered)
