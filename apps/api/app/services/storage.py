from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import TaskRecord
from app.services.db_store import PostgresTaskStore
from app.services.object_store import ObjectStorageError, ObjectStore
from app.services.review_engine import analyze_contract


SUPPORTED_TEXT_EXTENSIONS = {".md", ".txt", ".text"}


class ContractUploadError(ValueError):
    pass


class TaskStorageError(RuntimeError):
    pass


class TaskRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def list_tasks(self) -> list[TaskRecord]:
        self._bootstrap_if_needed()
        if self._use_postgres():
            return self._postgres_store().list_tasks()
        tasks = self._load_tasks()
        return sorted(tasks, key=lambda task: task.created_at, reverse=True)

    def get_task(self, task_id: str) -> TaskRecord | None:
        if self._use_postgres():
            self._bootstrap_if_needed()
            return self._postgres_store().get_task(task_id)
        return next((task for task in self.list_tasks() if task.id == task_id), None)

    def create_task_from_upload(
        self,
        filename: str,
        payload: bytes,
        contract_name: str | None = None,
        content_type: str | None = None,
    ) -> TaskRecord:
        if not payload:
            raise ContractUploadError("上传文件为空，请重新选择合同文件。")
        if len(payload) > self.settings.max_upload_bytes:
            limit_mb = self.settings.max_upload_bytes / 1024 / 1024
            raise ContractUploadError(f"上传文件超过 {limit_mb:.1f} MB 限制。")

        source_filename = sanitize_filename(filename or "contract.txt")
        suffix = Path(source_filename).suffix.lower()
        if suffix not in SUPPORTED_TEXT_EXTENSIONS:
            supported = "、".join(sorted(SUPPORTED_TEXT_EXTENSIONS))
            raise ContractUploadError(f"当前仅支持文本合同文件：{supported}。")

        contract_text = decode_contract_bytes(payload)
        task_id = f"task-{uuid.uuid4().hex[:10]}"
        try:
            stored_file = ObjectStore(self.settings).save_upload(
                task_id=task_id,
                filename=source_filename,
                payload=payload,
                content_type=content_type,
            )
        except ObjectStorageError as exc:
            raise TaskStorageError(str(exc)) from exc

        task = analyze_contract(
            task_id=task_id,
            source_filename=source_filename,
            contract_name=contract_name,
            contract_text=contract_text,
        ).model_copy(update={"stored_file": stored_file})
        if self._use_postgres():
            self._bootstrap_if_needed()
            self._postgres_store().upsert_task(task, event_message="uploaded contract and generated review snapshot")
        else:
            tasks = self.list_tasks()
            tasks.insert(0, task)
            self._save_tasks(tasks)
        return task

    def _bootstrap_if_needed(self) -> None:
        if self._use_postgres():
            store = self._postgres_store()
            if store.count_tasks() > 0:
                return
            for task in self._load_existing_json_tasks():
                store.upsert_task(task, event_message="imported task from json store")
            if store.count_tasks() > 0:
                return
            for task in self._build_sample_tasks():
                store.upsert_task(task, event_message="bootstrapped sample task")
            return

        if self.settings.task_store_path.exists():
            return

        self._ensure_dirs()
        self._save_tasks(self._build_sample_tasks())

    def _load_tasks(self) -> list[TaskRecord]:
        if not self.settings.task_store_path.exists():
            return []
        try:
            payload = json.loads(self.settings.task_store_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise TaskStorageError("任务存储文件格式不是列表。")
            return [TaskRecord.model_validate(item) for item in payload]
        except (json.JSONDecodeError, OSError, ValidationError, TaskStorageError) as exc:
            backup_path = self._backup_corrupt_store()
            raise TaskStorageError(f"任务存储文件无法读取，已备份到 {backup_path.name}。") from exc

    def _save_tasks(self, tasks: list[TaskRecord]) -> None:
        self._ensure_dirs()
        temp_path = self.settings.task_store_path.with_suffix(".tmp")
        payload = json.dumps(
            [task.model_dump() for task in tasks],
            ensure_ascii=False,
            indent=2,
        )
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self.settings.task_store_path)

    def _ensure_dirs(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)

    def _backup_corrupt_store(self) -> Path:
        self._ensure_dirs()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = self.settings.task_store_path.with_name(f"tasks.corrupt-{timestamp}.json")
        self.settings.task_store_path.replace(backup_path)
        self._save_tasks([])
        return backup_path

    def _build_sample_tasks(self) -> list[TaskRecord]:
        if not self.settings.bootstrap_samples or not self.settings.sample_contract_dir.exists():
            return []

        tasks: list[TaskRecord] = []
        for index, sample_path in enumerate(sorted(self.settings.sample_contract_dir.glob("*.md")), start=1):
            payload = sample_path.read_bytes()
            stored_file = ObjectStore(self.settings).save_upload(
                task_id=f"sample-{index:03d}",
                filename=sample_path.name,
                payload=payload,
                content_type="text/markdown",
            )
            task = analyze_contract(
                task_id=f"sample-{index:03d}",
                source_filename=sample_path.name,
                contract_name=sample_path.stem,
                contract_text=payload.decode("utf-8"),
            ).model_copy(update={"stored_file": stored_file})
            tasks.append(task)
        return tasks

    def _load_existing_json_tasks(self) -> list[TaskRecord]:
        if not self.settings.task_store_path.exists():
            return []
        return self._load_tasks()

    def _use_postgres(self) -> bool:
        return self.settings.task_store_backend == "postgres"

    def _postgres_store(self) -> PostgresTaskStore:
        return PostgresTaskStore(self.settings)


def decode_contract_bytes(payload: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text
    raise ContractUploadError("当前版本先支持 UTF-8/GBK 编码的文本合同（如 .md、.txt）。")


def sanitize_filename(filename: str) -> str:
    clean_name = Path(filename).name.strip()
    if not clean_name:
        return "contract.txt"
    return clean_name.replace("\x00", "")
