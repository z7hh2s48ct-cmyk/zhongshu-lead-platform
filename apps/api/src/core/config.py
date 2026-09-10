from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_name: str = "合家美宅客资平台"
    app_version: str = "1.2.5"
    log_level: str = "INFO"
    log_json: bool = True
    app_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./zhongshu.db"
    database_pool_size: int = 20
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 30
    web_concurrency: int = 1
    sync_threadpool_tokens: int = 40
    max_in_flight_requests: int = 32
    in_flight_queue_timeout_seconds: int = 10
    jwt_secret: str = "dev-change-me-at-least-32-characters-long"
    jwt_expire_minutes: int = 1440
    login_max_failed_attempts: int = 5
    login_failure_window_minutes: int = 15
    login_lock_minutes: int = 15
    field_encryption_key: str = "dev-only-key-change-in-production"
    phone_hash_secret: str = "dev-phone-hash-secret"
    phone_fingerprint_secret: str = ""
    lead_hard_duplicate_days: int = 90
    lead_reward_duplicate_days: int = 180
    lead_historical_suspect_days: int = 365
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    wechat_oauth_redirect_uri: str = "http://localhost:8000/api/v1/auth/wechat/callback"
    wechat_oauth_scope: str = "snsapi_base"
    wechat_dev_mock: bool = True
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_token: str = ""
    feishu_table_id: str = ""
    feishu_view_id: str = ""
    feishu_view_name: str = "客户视图"
    feishu_dev_mock: bool = True
    feishu_field_mapping_json: str = ""
    feishu_sync_page_size: int = 200
    feishu_sync_max_pages: int = 100
    feishu_writeback_enabled: bool = False
    object_storage_backend: str = "local"
    object_storage_dir: str = "./storage"
    lead_export_active_per_user_limit: int = 2
    lead_export_active_global_limit: int = 20
    lead_export_rolling_24h_per_user_limit: int = 10
    s3_endpoint_url: str = "https://cos.ap-shanghai.myqcloud.com"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "ap-shanghai"
    cors_origins: str = "http://localhost:8000"
    low_points_warning_threshold: int = 1000
    assignment_reminder_hours: int = 24
    assignment_expire_hours: int = 48
    pre_dispatch_verification_hours: int = 24
    return_verification_hours: int = 24
    # Continuous return window used by the legacy endpoint; V1.2 fixes the same
    # product rule at 48 hours from the persisted claim timestamp.
    return_window_hours: int = 48
    # Development/test may keep legacy mutation APIs for historical regression tests.
    # Production validation requires this to be false so all new business facts use V1.2.
    legacy_write_enabled: bool = True
    first_followup_hours: int = 48
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    # N1：仅当 API 确实部署在会强制覆写 x-real-ip 的受信反向代理之后时才
    # 开启；默认关闭——直连或代理未覆写该头时，客户端伪造的头绝不能充当
    # 客户端身份（限流键、审计 IP 均会被绕过/污染）。
    trust_proxy_headers: bool = False
    auto_create_schema: bool = True

    @field_validator("object_storage_dir")
    @classmethod
    def normalize_storage_dir(cls, value: str) -> str:
        return str(Path(value))

    @field_validator(
        "lead_hard_duplicate_days",
        "lead_reward_duplicate_days",
        "lead_historical_suspect_days",
        "login_max_failed_attempts",
        "login_failure_window_minutes",
        "login_lock_minutes",
        "pre_dispatch_verification_hours",
        "return_verification_hours",
        "lead_export_active_per_user_limit",
        "lead_export_active_global_limit",
        "lead_export_rolling_24h_per_user_limit",
    )
    @classmethod
    def validate_positive_window(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("窗口/阈值配置必须大于 0")
        return value

    @field_validator("database_pool_size")
    @classmethod
    def validate_database_pool_size(cls, value: int) -> int:
        if not 1 <= value <= 40:
            raise ValueError("DATABASE_POOL_SIZE must be between 1 and 40")
        return value

    @field_validator("database_max_overflow")
    @classmethod
    def validate_database_max_overflow(cls, value: int) -> int:
        if not 0 <= value <= 40:
            raise ValueError("DATABASE_MAX_OVERFLOW must be between 0 and 40")
        return value

    @field_validator("database_pool_timeout_seconds")
    @classmethod
    def validate_database_pool_timeout_seconds(cls, value: int) -> int:
        if not 1 <= value <= 60:
            raise ValueError("DATABASE_POOL_TIMEOUT_SECONDS must be between 1 and 60")
        return value

    @field_validator("web_concurrency")
    @classmethod
    def validate_web_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("WEB_CONCURRENCY must be between 1 and 5")
        return value

    @field_validator("sync_threadpool_tokens")
    @classmethod
    def validate_sync_threadpool_tokens(cls, value: int) -> int:
        if not 1 <= value <= 40:
            raise ValueError("SYNC_THREADPOOL_TOKENS must be between 1 and 40")
        return value

    @field_validator("max_in_flight_requests")
    @classmethod
    def validate_max_in_flight_requests(cls, value: int) -> int:
        if not 1 <= value <= 40:
            raise ValueError("MAX_IN_FLIGHT_REQUESTS must be between 1 and 40")
        return value

    @field_validator("in_flight_queue_timeout_seconds")
    @classmethod
    def validate_in_flight_queue_timeout_seconds(cls, value: int) -> int:
        if not 1 <= value <= 30:
            raise ValueError("IN_FLIGHT_QUEUE_TIMEOUT_SECONDS must be between 1 and 30")
        return value

    @model_validator(mode="after")
    def validate_database_connection_budget(self) -> "Settings":
        per_process_budget = self.database_pool_size + self.database_max_overflow
        if per_process_budget > 40:
            raise ValueError("DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW must not exceed 40 per process")
        if self.sync_threadpool_tokens > per_process_budget:
            raise ValueError("SYNC_THREADPOOL_TOKENS must not exceed the per-process database connection budget")
        if self.max_in_flight_requests > per_process_budget:
            raise ValueError("MAX_IN_FLIGHT_REQUESTS must not exceed the per-process database connection budget")
        if self.web_concurrency * per_process_budget > 90:
            raise ValueError("WEB_CONCURRENCY times the per-process database connection budget must not exceed 90")
        return self

    @property
    def effective_phone_fingerprint_secret(self) -> str:
        return self.phone_fingerprint_secret or self.phone_hash_secret

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
