from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
API_ROOT = APP_DIR.parent
REPO_ROOT = API_ROOT.parent.parent
_LOADED_ENV_PATH: Path | None = None
_LOADED_ENV_VALUES: dict[str, str] = {}


def _parse_env_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None

    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if value and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _candidate_env_paths() -> list[Path]:
    cwd = Path.cwd().resolve()
    return [
        cwd / ".env",
        cwd.parent / ".env",
        cwd.parent.parent / ".env",
        REPO_ROOT / ".env",
    ]


def bootstrap_environment() -> Path | None:
    global _LOADED_ENV_PATH, _LOADED_ENV_VALUES

    if _LOADED_ENV_PATH is not None:
        return _LOADED_ENV_PATH

    seen: set[Path] = set()
    for candidate in _candidate_env_paths():
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw_line)
            if parsed is None:
                continue
            key, value = parsed
            _LOADED_ENV_VALUES[key] = value
            os.environ.setdefault(key, value)
        _LOADED_ENV_PATH = candidate
        return _LOADED_ENV_PATH
    return None


bootstrap_environment()


def _flag(name: str, default: bool) -> bool:
    raw = _setting(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _setting(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    file_value = _LOADED_ENV_VALUES.get(name)
    if raw in {None, "", "replace_me"} and file_value not in {None, ""}:
        return file_value
    return raw if raw not in {None, ""} else default


def _first_setting(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = _setting(name)
        if value not in {None, ""}:
            return value
    return default


def _normalize_database_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _normalize_minio_endpoint(endpoint: str | None, public_endpoint: str | None = None) -> str:
    raw = endpoint or public_endpoint or "http://127.0.0.1:9000"
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


def _max_upload_bytes() -> int:
    explicit_bytes = _setting("CONTRACT_COMPLIANCE_MAX_UPLOAD_BYTES")
    if explicit_bytes:
        return int(explicit_bytes)
    max_mb = _setting("MAX_UPLOAD_MB")
    if max_mb:
        return int(float(max_mb) * 1024 * 1024)
    return 2 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    upload_dir: Path
    task_store_path: Path
    task_store_backend: str
    database_url: str | None
    sample_contract_dir: Path
    env_file_path: str | None
    object_storage_backend: str
    minio_endpoint_url: str
    minio_access_key: str | None
    minio_secret_key: str | None
    minio_bucket: str
    minio_secure: bool
    ragflow_base_url: str
    ragflow_api_key: str | None
    bootstrap_samples: bool
    max_upload_bytes: int
    llm_base_url: str
    llm_chat_completions_url: str | None
    llm_api_key: str | None
    llm_model: str
    llm_timeout_seconds: float
    llm_probe_enabled: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file_path = str(_LOADED_ENV_PATH) if _LOADED_ENV_PATH is not None else None
    data_dir = Path(_first_setting("CONTRACT_COMPLIANCE_DATA_DIR", default=str(API_ROOT / "data")))
    upload_dir = Path(_first_setting("CONTRACT_COMPLIANCE_UPLOAD_DIR", "UPLOAD_DIR", default=str(data_dir / "uploads")))
    task_store_path = Path(_first_setting("CONTRACT_COMPLIANCE_TASK_STORE", default=str(data_dir / "tasks.json")))
    task_store_backend = _first_setting("CONTRACT_COMPLIANCE_TASK_STORE_BACKEND", "TASK_STORE_BACKEND", default="json").strip().lower()
    database_url = _normalize_database_url(
        _first_setting("CONTRACT_COMPLIANCE_DATABASE_URL", "DATABASE_URL")
    )
    sample_contract_dir = Path(
        _first_setting(
            "CONTRACT_COMPLIANCE_SAMPLE_DIR",
            default=str(REPO_ROOT / "resource" / "01_合同样本"),
        )
    )
    object_storage_backend = _first_setting("CONTRACT_COMPLIANCE_OBJECT_STORAGE", "STORAGE_BACKEND", default="local").strip().lower()
    minio_endpoint_url = _normalize_minio_endpoint(
        _first_setting("MINIO_ENDPOINT_URL", "MINIO_ENDPOINT"),
        _setting("MINIO_PUBLIC_ENDPOINT"),
    )
    minio_access_key = _first_setting("MINIO_ACCESS_KEY", "MINIO_ROOT_USER")
    minio_secret_key = _first_setting("MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD")
    minio_bucket = _first_setting(
        "MINIO_BUCKET",
        "MINIO_BUCKET_CONTRACTS",
        default="contract-compliance",
    )
    minio_secure = _flag("MINIO_SECURE", False)
    ragflow_base_url = _setting("RAGFLOW_BASE_URL", "http://127.0.0.1:9380").rstrip("/")
    ragflow_api_key = _setting("RAGFLOW_API_KEY")
    bootstrap_samples = _flag("CONTRACT_COMPLIANCE_BOOTSTRAP_SAMPLES", True)
    max_upload_bytes = _max_upload_bytes()
    llm_base_url = _setting("LLM_BASE_URL", "https://gen.trendbot.cn/v1").rstrip("/")
    llm_chat_completions_url = _setting("LLM_CHAT_COMPLETIONS_URL")
    llm_api_key = _setting("LLM_API_KEY")
    llm_model = _setting("LLM_MODEL", "I2AI/minimax-m2.5")
    llm_timeout_seconds = float(_setting("LLM_TIMEOUT_SECONDS", "15"))
    llm_probe_enabled = _flag("LLM_PROBE_ENABLED", False)
    return Settings(
        data_dir=data_dir,
        upload_dir=upload_dir,
        task_store_path=task_store_path,
        task_store_backend=task_store_backend,
        database_url=database_url,
        sample_contract_dir=sample_contract_dir,
        env_file_path=env_file_path,
        object_storage_backend=object_storage_backend,
        minio_endpoint_url=minio_endpoint_url,
        minio_access_key=minio_access_key,
        minio_secret_key=minio_secret_key,
        minio_bucket=minio_bucket,
        minio_secure=minio_secure,
        ragflow_base_url=ragflow_base_url,
        ragflow_api_key=ragflow_api_key,
        bootstrap_samples=bootstrap_samples,
        max_upload_bytes=max_upload_bytes,
        llm_base_url=llm_base_url,
        llm_chat_completions_url=llm_chat_completions_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_probe_enabled=llm_probe_enabled,
    )
