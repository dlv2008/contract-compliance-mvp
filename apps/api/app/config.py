from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
API_ROOT = APP_DIR.parent
REPO_ROOT = API_ROOT.parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    upload_dir: Path
    task_store_path: Path
    sample_contract_dir: Path
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
    data_dir = Path(os.getenv("CONTRACT_COMPLIANCE_DATA_DIR", API_ROOT / "data"))
    upload_dir = Path(os.getenv("CONTRACT_COMPLIANCE_UPLOAD_DIR", data_dir / "uploads"))
    task_store_path = Path(os.getenv("CONTRACT_COMPLIANCE_TASK_STORE", data_dir / "tasks.json"))
    sample_contract_dir = Path(
        os.getenv(
            "CONTRACT_COMPLIANCE_SAMPLE_DIR",
            REPO_ROOT / "resource" / "01_合同样本",
        )
    )
    ragflow_base_url = os.getenv("RAGFLOW_BASE_URL", "http://127.0.0.1:9380").rstrip("/")
    ragflow_api_key = os.getenv("RAGFLOW_API_KEY")
    bootstrap_samples = _flag("CONTRACT_COMPLIANCE_BOOTSTRAP_SAMPLES", True)
    max_upload_bytes = int(os.getenv("CONTRACT_COMPLIANCE_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024)))
    llm_base_url = os.getenv("LLM_BASE_URL", "https://gen.trendbot.cn/v1").rstrip("/")
    llm_chat_completions_url = os.getenv("LLM_CHAT_COMPLETIONS_URL")
    llm_api_key = os.getenv("LLM_API_KEY")
    llm_model = os.getenv("LLM_MODEL", "I2AI/minimax-m2.5")
    llm_timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "15"))
    llm_probe_enabled = _flag("LLM_PROBE_ENABLED", False)
    return Settings(
        data_dir=data_dir,
        upload_dir=upload_dir,
        task_store_path=task_store_path,
        sample_contract_dir=sample_contract_dir,
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
