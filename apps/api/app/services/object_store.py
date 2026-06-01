from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.models import ObjectStorageProbe, StoredFile


class ObjectStorageError(RuntimeError):
    pass


class ObjectStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def save_upload(
        self,
        *,
        task_id: str,
        filename: str,
        payload: bytes,
        content_type: str | None = None,
    ) -> StoredFile:
        backend = self._resolved_backend()
        if backend == "minio":
            return self._save_to_minio(task_id, filename, payload, content_type)
        return self._save_to_local(task_id, filename, payload, content_type)

    def probe(self) -> ObjectStorageProbe:
        backend = self._resolved_backend()
        if backend == "local":
            try:
                self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return ObjectStorageProbe(
                    backend="local",
                    status="error",
                    detail=f"本地原件目录不可用：{exc}",
                    healthy=False,
                )
            return ObjectStorageProbe(
                backend="local",
                status="ok",
                detail=f"本地原件目录可用：{self.settings.upload_dir}",
                healthy=True,
            )

        if not self._minio_configured():
            return ObjectStorageProbe(
                backend="minio",
                status="not_configured",
                detail="MinIO endpoint/access key/secret key 未配置完整。",
                healthy=False,
                endpoint_url=self.settings.minio_endpoint_url,
                bucket=self.settings.minio_bucket,
            )

        try:
            client = self._s3_client()
            self._ensure_bucket(client)
        except Exception as exc:
            return ObjectStorageProbe(
                backend="minio",
                status="error",
                detail=f"MinIO 连接或 bucket 检查失败：{exc.__class__.__name__}",
                healthy=False,
                endpoint_url=self.settings.minio_endpoint_url,
                bucket=self.settings.minio_bucket,
            )
        return ObjectStorageProbe(
            backend="minio",
            status="ok",
            detail=f"MinIO bucket 可用：{self.settings.minio_bucket}",
            healthy=True,
            endpoint_url=self.settings.minio_endpoint_url,
            bucket=self.settings.minio_bucket,
        )

    def _save_to_local(
        self,
        task_id: str,
        filename: str,
        payload: bytes,
        content_type: str | None,
    ) -> StoredFile:
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _sanitize_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        target_path = self.settings.upload_dir / f"{task_id}{suffix}"
        target_path.write_bytes(payload)
        return self._stored_file(
            filename=safe_name,
            payload=payload,
            content_type=content_type,
            backend="local",
            object_key=target_path.name,
            local_path=str(target_path),
            bucket=None,
        )

    def _save_to_minio(
        self,
        task_id: str,
        filename: str,
        payload: bytes,
        content_type: str | None,
    ) -> StoredFile:
        if not self._minio_configured():
            raise ObjectStorageError("MinIO 配置不完整，无法保存原件。")

        client = self._s3_client()
        self._ensure_bucket(client)
        safe_name = _sanitize_filename(filename)
        object_key = f"uploads/{task_id}/original-{safe_name}"
        client.put_object(
            Bucket=self.settings.minio_bucket,
            Key=object_key,
            Body=payload,
            ContentType=content_type or "application/octet-stream",
            Metadata={"task_id": task_id, "sha256": _sha256(payload)},
        )
        return self._stored_file(
            filename=safe_name,
            payload=payload,
            content_type=content_type,
            backend="minio",
            object_key=object_key,
            local_path=None,
            bucket=self.settings.minio_bucket,
        )

    def _resolved_backend(self) -> str:
        backend = self.settings.object_storage_backend
        if backend == "auto":
            return "minio" if self._minio_configured() else "local"
        if backend in {"minio", "s3"}:
            return "minio"
        return "local"

    def _minio_configured(self) -> bool:
        return bool(
            self.settings.minio_endpoint_url
            and self.settings.minio_access_key
            and self.settings.minio_secret_key
            and self.settings.minio_bucket
        )

    def _s3_client(self) -> Any:
        try:
            import boto3
        except ImportError as exc:
            raise ObjectStorageError("缺少 boto3 依赖，无法连接 MinIO。") from exc

        return boto3.client(
            "s3",
            endpoint_url=self.settings.minio_endpoint_url,
            aws_access_key_id=self.settings.minio_access_key,
            aws_secret_access_key=self.settings.minio_secret_key,
            region_name="us-east-1",
            use_ssl=self.settings.minio_secure,
        )

    def _ensure_bucket(self, client: Any) -> None:
        try:
            client.head_bucket(Bucket=self.settings.minio_bucket)
        except Exception:
            client.create_bucket(Bucket=self.settings.minio_bucket)

    def _stored_file(
        self,
        *,
        filename: str,
        payload: bytes,
        content_type: str | None,
        backend: str,
        object_key: str,
        local_path: str | None,
        bucket: str | None,
    ) -> StoredFile:
        return StoredFile(
            original_filename=filename,
            content_type=content_type or "application/octet-stream",
            size_bytes=len(payload),
            sha256=_sha256(payload),
            storage_backend=backend,
            object_key=object_key,
            local_path=local_path,
            bucket=bucket,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sanitize_filename(filename: str) -> str:
    clean_name = Path(filename).name.strip()
    if not clean_name:
        return "contract.txt"
    return clean_name.replace("\x00", "")
