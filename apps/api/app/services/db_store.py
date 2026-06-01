from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings, get_settings
from app.models import DatabaseProbe, TaskRecord


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_task (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    overall_risk TEXT NOT NULL,
    decision TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_task_created_at ON review_task (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_task_status ON review_task (status);
CREATE INDEX IF NOT EXISTS idx_review_task_risk ON review_task (overall_risk);

CREATE TABLE IF NOT EXISTS audit_event (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT REFERENCES review_task(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_event_task_id ON audit_event (task_id, created_at DESC);
"""


class PostgresUnavailableError(RuntimeError):
    pass


class PostgresTaskStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.database_url:
            raise PostgresUnavailableError("未配置 CONTRACT_COMPLIANCE_DATABASE_URL。")
        self.ensure_schema()

    def list_tasks(self) -> list[TaskRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM review_task ORDER BY created_at DESC")
                return [TaskRecord.model_validate(row[0]) for row in cur.fetchall()]

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM review_task WHERE id = %s", (task_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return TaskRecord.model_validate(row[0])

    def upsert_task(self, task: TaskRecord, event_message: str = "task upserted") -> None:
        from psycopg.types.json import Jsonb

        payload = task.model_dump()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO review_task (
                        id, name, status, overall_risk, decision, source_filename, created_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        status = EXCLUDED.status,
                        overall_risk = EXCLUDED.overall_risk,
                        decision = EXCLUDED.decision,
                        source_filename = EXCLUDED.source_filename,
                        created_at = EXCLUDED.created_at,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    (
                        task.id,
                        task.name,
                        task.status,
                        task.overall_risk,
                        task.decision,
                        task.source_filename,
                        task.created_at,
                        Jsonb(payload),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO audit_event (task_id, event_type, message, payload, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        task.id,
                        "task.snapshot",
                        event_message,
                        Jsonb({"status": task.status, "overall_risk": task.overall_risk}),
                        datetime.now(timezone.utc),
                    ),
                )
            conn.commit()

    def count_tasks(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM review_task")
                return int(cur.fetchone()[0])

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresUnavailableError("缺少 psycopg 依赖，无法连接 PostgreSQL。") from exc
        return psycopg.connect(self.settings.database_url, connect_timeout=5)


class DatabaseProbeClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def probe(self) -> DatabaseProbe:
        backend = self.settings.task_store_backend
        if backend != "postgres":
            return DatabaseProbe(
                backend="json",
                status="ok",
                detail=f"当前使用 JSON 任务仓储：{self.settings.task_store_path}",
                healthy=True,
                task_count=self._json_task_count(),
            )
        if not self.settings.database_url:
            return DatabaseProbe(
                backend="postgres",
                status="not_configured",
                detail="未配置 CONTRACT_COMPLIANCE_DATABASE_URL。",
                healthy=False,
            )
        try:
            store = PostgresTaskStore(self.settings)
            task_count = store.count_tasks()
        except Exception as exc:
            return DatabaseProbe(
                backend="postgres",
                status="error",
                detail=f"PostgreSQL 检查失败：{exc.__class__.__name__}",
                healthy=False,
                dsn=mask_dsn(self.settings.database_url),
            )
        return DatabaseProbe(
            backend="postgres",
            status="ok",
            detail=f"PostgreSQL 可用，review_task 中已有 {task_count} 条任务。",
            healthy=True,
            dsn=mask_dsn(self.settings.database_url),
            task_count=task_count,
        )

    def _json_task_count(self) -> int:
        if not self.settings.task_store_path.exists():
            return 0
        try:
            import json

            payload = json.loads(self.settings.task_store_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        return len(payload) if isinstance(payload, list) else 0


def mask_dsn(dsn: str | None) -> str | None:
    if not dsn:
        return None
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    username = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
