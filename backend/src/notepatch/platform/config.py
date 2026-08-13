from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "NotePatch Learning Backend"
    environment: str = "local"
    secret_key: str = "change-me-in-production-use-at-least-32-bytes"
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30
    refresh_token_rotation_grace_seconds: int = 10
    admin_emails: str = ""
    admin_web_origin: str = "http://localhost:5173"

    database_url: str = "postgresql+psycopg://notepatch:notepatch@postgres:5432/notepatch"
    redis_url: str = "redis://redis:6379/0"
    redis_task_queue: str = "notepatch:tasks"
    redis_task_retry_queue: str = "notepatch:task-retries"
    default_queue_name: str = "default"
    ocr_queue_name: str = "ocr"
    chat_queue_name: str = "chat"
    worker_queues: str = "default"
    ocr_worker_queues: str = "ocr"
    chat_worker_queues: str = "chat"
    presence_heartbeat_interval_seconds: int = 30
    presence_session_ttl_seconds: int = 90
    presence_offline_grace_seconds: int = 600
    task_max_attempts: int = 3
    task_retry_base_seconds: int = 5
    task_retry_max_seconds: int = 300
    task_worker_lease_seconds: int = 60
    task_orphan_recovery_grace_seconds: int = 90
    task_orphan_recovery_interval_seconds: int = 30
    purge_task_max_attempts: int = 20
    task_cancellation_grace_seconds: int = 600
    gpu_lock_key: str = "notepatch:gpu:lease"
    gpu_lock_enabled: bool = True
    gpu_lock_wait_seconds: int = 600
    gpu_lock_lease_seconds: int = 900

    seaweedfs_s3_endpoint: str = "http://seaweedfs-s3:8333"
    seaweedfs_access_key: str = "notepatch"
    seaweedfs_secret_key: str = "notepatch-secret"
    seaweedfs_bucket: str = "notepatch"
    seaweedfs_public_base_url: str = "http://localhost:8333"
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_secure: bool = False
    presign_expire_seconds: int = 3600
    public_api_base_url: str = ""
    backend_cors_origins: str = "*"

    tusd_base_url: str = "http://localhost:1080/files/"
    tusd_internal_base_url: str = "http://tusd:1080/files/"
    tusd_webhook_secret: str = "change-me-tusd-webhook-secret"
    tusd_data_dir: str = "/tusd-data"
    upload_max_file_size_mb: int = 200
    upload_allowed_mime_types: str = (
        "image/jpeg,image/png,image/webp,image/tiff,application/pdf,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    clamav_enabled: bool = False
    clamav_host: str = "clamav"
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 120
    converter_base_url: str = "http://converter:8000"
    converter_timeout_seconds: float = 180

    openclaw_workdir: str = "/tmp/notepatch-openclaw"
    openclaw_gateway_base_url: str = "http://host.docker.internal:18789"
    openclaw_gateway_token: str | None = None
    openclaw_gateway_model: str = "openclaw"
    openclaw_agent_model: str = "openai/gpt-5.4"
    openclaw_gateway_timeout_seconds: float = 120
    openclaw_skill_timeout_seconds: float = 300
    openclaw_gateway_ready_timeout_seconds: float = 30
    openclaw_gateway_ready_poll_seconds: float = 2
    openclaw_gateway_scopes: str = "operator.write"
    notepatch_data_root: str = "/home/usr/notepatch-data"
    openclaw_asset_root: str = "/opt/notepatch/openclaw"
    openclaw_user_runtime_root: str = "/home/usr/notepatch-data/openclaw"
    openclaw_docker_network: str = "notepatch-server_default"
    openclaw_user_gateway_image: str = "openclaw-webui-node-docker:local"
    openclaw_user_gateway_autostart: bool = False
    openclaw_user_gateway_token_prefix: str = "notepatch"
    openclaw_user_runtime_uid: int = 1000
    openclaw_user_runtime_gid: int = 1000
    openclaw_docker_socket_gid: int | None = None
    openclaw_supervisor_poll_seconds: float = 10
    openclaw_supervisor_container_stop_timeout_seconds: int = 20
    openclaw_gateway_memory_limit: str = "2g"
    openclaw_gateway_nano_cpus: int = 1_500_000_000
    openclaw_gateway_pids_limit: int = 256
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_organization: str | None = None
    openai_project: str | None = None
    ai_model_catalog_ttl_seconds: int = 300
    ai_provider_timeout_seconds: float = 15
    ai_model_allowlist: str = ""
    ai_chat_history_message_limit: int = 20
    study_note_debounce_seconds: int = 300
    knowledge_point_match_threshold: float = 0.88
    flashcard_error_half_life_days: float = 30.0
    flashcard_success_half_life_days: float = 14.0
    flashcard_error_multiplier: float = 2.5
    flashcard_success_multiplier: float = 0.8
    flashcard_correct_streak_multiplier: float = 0.7
    flashcard_max_correct_streak: int = 5
    flashcard_max_cards: int = 40
    note_highlight_red_threshold: float = 3.0
    note_highlight_yellow_threshold: float = 1.5

    doctr_enabled: bool = True
    doctr_base_url: str = "http://docserver:8000"
    doctr_timeout_seconds: float = 300
    doctr_ill_rec: bool = True

    ocr_engine: str = "paddleocr"
    ocr_temp_dir: str = "/tmp/ocr"
    ocr_max_pages: int = 50
    ocr_max_file_size_mb: int = 200
    ocr_render_dpi: int = 200
    ocr_save_page_images: bool = False
    ocr_enable_preprocess: bool = True
    ocr_enable_layout: bool = True
    ocr_enable_formula: bool = True
    ocr_enable_table: bool = True
    ocr_worker_concurrency: int = 1
    ocr_task_timeout_seconds: int = 300

    paddleocr_use_gpu: bool = True
    paddleocr_lang: str = "ch"
    paddleocr_det_model_dir: str | None = None
    paddleocr_rec_model_dir: str | None = None
    paddleocr_cls_model_dir: str | None = None
    paddleocr_structure_model: str = "PP-StructureV3"
    paddleocr_formula_model: str = "PP-FormulaNet_plus-M"

    embedding_service_url: str = "http://embedding-service:8000"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = 1024
    embedding_timeout_seconds: int = 300
    knowledge_search_limit: int = 6

    auto_learning_pipeline: bool = True
    task_sse_poll_seconds: float = 1.0
    task_sse_heartbeat_seconds: float = 15.0
    note_render_token_expire_seconds: int = 900
    metrics_token: str | None = None
    release_revision: str = "dev"
    release_build_time: str = "unknown"
    schema_revision: str = "202608140001"
    rate_limit_enabled: bool = False
    auth_rate_limit_per_minute: int = 20
    upload_rate_limit_per_minute: int = 30
    ai_rate_limit_per_minute: int = 20

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        if self.backend_cors_origins.strip() == "*":
            return ["*"]
        origins = [origin.strip() for origin in self.backend_cors_origins.split(",") if origin.strip()]
        if self.admin_web_origin and self.admin_web_origin not in origins:
            origins.append(self.admin_web_origin)
        return origins

    @property
    def admin_email_set(self) -> set[str]:
        return {email.strip().lower() for email in self.admin_emails.split(",") if email.strip()}

    @property
    def ai_model_allowlist_set(self) -> set[str]:
        return {
            model.strip()
            for model in self.ai_model_allowlist.split(",")
            if model.strip()
        }

    @property
    def upload_allowed_mime_type_set(self) -> set[str]:
        return {value.strip().lower() for value in self.upload_allowed_mime_types.split(",") if value.strip()}


    @property
    def effective_secret_key(self) -> str:
        return self.jwt_secret or self.secret_key

    @property
    def storage_endpoint_url(self) -> str:
        return self.s3_endpoint_url or self.seaweedfs_s3_endpoint

    @property
    def storage_access_key(self) -> str:
        return self.s3_access_key or self.seaweedfs_access_key

    @property
    def storage_secret_key(self) -> str:
        return self.s3_secret_key or self.seaweedfs_secret_key

    @property
    def storage_bucket(self) -> str:
        return self.s3_bucket or self.seaweedfs_bucket

    @property
    def storage_public_base_url(self) -> str:
        return self.seaweedfs_public_base_url or self.storage_endpoint_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
