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

CREATE TABLE IF NOT EXISTS ragflow_document (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    dataset_id TEXT,
    document_id TEXT,
    upload_status TEXT NOT NULL DEFAULT 'pending',
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parser_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ragflow_document_task_id ON ragflow_document (task_id);

CREATE TABLE IF NOT EXISTS document_clause (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    sequence_no INTEGER NOT NULL DEFAULT 0,
    parser_source TEXT NOT NULL DEFAULT 'local',
    chunk_id TEXT,
    page_start INTEGER,
    page_end INTEGER,
    positions JSONB NOT NULL DEFAULT '{}'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, clause_id, version)
);

CREATE INDEX IF NOT EXISTS idx_document_clause_task_id ON document_clause (task_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_document_clause_chunk_id ON document_clause (chunk_id);

CREATE TABLE IF NOT EXISTS extracted_fact (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    fact_key TEXT NOT NULL,
    label TEXT NOT NULL,
    value_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'present',
    evidence_clause_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    extractor TEXT NOT NULL DEFAULT 'deterministic-mvp',
    schema_version TEXT NOT NULL DEFAULT 'mvp-facts-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, fact_key, schema_version)
);

CREATE INDEX IF NOT EXISTS idx_extracted_fact_task_id ON extracted_fact (task_id, fact_key);

CREATE TABLE IF NOT EXISTS rule_hit (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL DEFAULT 'mvp-rules-v1',
    title TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_clause_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    policy_reference_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    action TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT 'deterministic',
    review_status TEXT NOT NULL DEFAULT 'pending',
    reviewer_comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, rule_id, rule_version)
);

CREATE INDEX IF NOT EXISTS idx_rule_hit_task_id ON rule_hit (task_id, level);
CREATE INDEX IF NOT EXISTS idx_rule_hit_review_status ON rule_hit (review_status);

CREATE TABLE IF NOT EXISTS review_action (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'reviewer',
    comment TEXT,
    revised_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_action_task_id ON review_action (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_action_target ON review_action (task_id, target_type, target_id);

CREATE TABLE IF NOT EXISTS report_snapshot (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'process_snapshot',
    generated_by TEXT NOT NULL DEFAULT 'system',
    rule_version TEXT NOT NULL DEFAULT 'mvp-rules-v1',
    source_file_sha256 TEXT,
    file_path TEXT,
    file_sha256 TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, version)
);

CREATE INDEX IF NOT EXISTS idx_report_snapshot_task_id ON report_snapshot (task_id, version DESC);

ALTER TABLE report_snapshot ADD COLUMN IF NOT EXISTS report_type TEXT NOT NULL DEFAULT 'process_snapshot';
ALTER TABLE report_snapshot ADD COLUMN IF NOT EXISTS generated_by TEXT NOT NULL DEFAULT 'system';
"""


class PostgresUnavailableError(RuntimeError):
    pass


class PostgresTaskStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.database_url:
            raise PostgresUnavailableError("未配置 DATABASE_URL。")
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
                self._sync_task_children(cur, task)
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

    def list_document_clauses(self, task_id: str) -> list[dict]:
        return self._select_dicts(
            """
            SELECT task_id, clause_id, title, text, status, sequence_no, parser_source,
                   chunk_id, page_start, page_end, positions, version, created_at, updated_at
            FROM document_clause
            WHERE task_id = %s
            ORDER BY sequence_no, clause_id
            """,
            (task_id,),
        )

    def list_extracted_facts(self, task_id: str) -> list[dict]:
        return self._select_dicts(
            """
            SELECT task_id, fact_key, label, value_text AS value, status, evidence_clause_ids,
                   extractor, schema_version, created_at, updated_at
            FROM extracted_fact
            WHERE task_id = %s
            ORDER BY fact_key
            """,
            (task_id,),
        )

    def list_rule_hits(self, task_id: str) -> list[dict]:
        return self._select_dicts(
            """
            SELECT task_id, rule_id, rule_version, title, level, message, reason,
                   evidence_clause_ids, policy_reference_ids, action, engine,
                   review_status, reviewer_comment, created_at, updated_at
            FROM rule_hit
            WHERE task_id = %s
            ORDER BY CASE level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC, rule_id
            """,
            (task_id,),
        )

    def list_review_actions(self, task_id: str) -> list[dict]:
        return self._select_dicts(
            """
            SELECT id, task_id, target_type, target_id, action_type, actor, comment,
                   revised_payload, created_at
            FROM review_action
            WHERE task_id = %s
            ORDER BY created_at DESC
            """,
            (task_id,),
        )

    def list_report_snapshots(self, task_id: str) -> list[dict]:
        return self._select_dicts(
            """
            SELECT task_id, version, title, summary, recommendation, rule_version,
                   report_type, generated_by, source_file_sha256, file_path,
                   file_sha256, generated_at, created_at
            FROM report_snapshot
            WHERE task_id = %s
            ORDER BY version DESC
            """,
            (task_id,),
        )

    def count_tasks(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM review_task")
                return int(cur.fetchone()[0])

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                self._backfill_child_tables(cur)
            conn.commit()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresUnavailableError("缺少 psycopg 依赖，无法连接 PostgreSQL。") from exc
        return psycopg.connect(self.settings.database_url, connect_timeout=5)

    def _select_dicts(self, query: str, params: tuple) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [column.name for column in cur.description]
                return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    def _sync_task_children(self, cur, task: TaskRecord) -> None:  # noqa: ANN001
        from psycopg.types.json import Jsonb

        cur.execute("DELETE FROM document_clause WHERE task_id = %s", (task.id,))
        for index, clause in enumerate(task.clauses, start=1):
            cur.execute(
                """
                INSERT INTO document_clause (
                    task_id, clause_id, title, text, status, sequence_no, positions, version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, clause_id, version) DO UPDATE SET
                    title = EXCLUDED.title,
                    text = EXCLUDED.text,
                    status = EXCLUDED.status,
                    sequence_no = EXCLUDED.sequence_no,
                    positions = EXCLUDED.positions,
                    updated_at = now()
                """,
                (task.id, clause.id, clause.title, clause.text, clause.status, index, Jsonb({}), 1),
            )

        cur.execute("DELETE FROM extracted_fact WHERE task_id = %s", (task.id,))
        for field in task.extracted_fields:
            cur.execute(
                """
                INSERT INTO extracted_fact (
                    task_id, fact_key, label, value_text, status, evidence_clause_ids,
                    extractor, schema_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, fact_key, schema_version) DO UPDATE SET
                    label = EXCLUDED.label,
                    value_text = EXCLUDED.value_text,
                    status = EXCLUDED.status,
                    evidence_clause_ids = EXCLUDED.evidence_clause_ids,
                    updated_at = now()
                """,
                (
                    task.id,
                    field.key,
                    field.label,
                    field.value,
                    field.status,
                    Jsonb(field.evidence_clause_ids),
                    "deterministic-mvp",
                    "mvp-facts-v1",
                ),
            )

        cur.execute("DELETE FROM rule_hit WHERE task_id = %s", (task.id,))
        for risk in task.risks:
            cur.execute(
                """
                INSERT INTO rule_hit (
                    task_id, rule_id, rule_version, title, level, message, reason,
                    evidence_clause_ids, policy_reference_ids, action, engine,
                    review_status, reviewer_comment
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, rule_id, rule_version) DO UPDATE SET
                    title = EXCLUDED.title,
                    level = EXCLUDED.level,
                    message = EXCLUDED.message,
                    reason = EXCLUDED.reason,
                    evidence_clause_ids = EXCLUDED.evidence_clause_ids,
                    policy_reference_ids = EXCLUDED.policy_reference_ids,
                    action = EXCLUDED.action,
                    engine = EXCLUDED.engine,
                    review_status = EXCLUDED.review_status,
                    reviewer_comment = EXCLUDED.reviewer_comment,
                    updated_at = now()
                """,
                (
                    task.id,
                    risk.rule_id,
                    risk.rule_version,
                    risk.title,
                    risk.level,
                    risk.message,
                    risk.reason,
                    Jsonb(risk.evidence_clause_ids),
                    Jsonb(risk.policy_reference_ids),
                    risk.action,
                    "deterministic",
                    risk.review_status,
                    risk.reviewer_comment,
                ),
            )

        for action in task.review_actions:
            cur.execute(
                """
                INSERT INTO review_action (
                    id, task_id, target_type, target_id, action_type, actor, comment,
                    revised_payload, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    target_type = EXCLUDED.target_type,
                    target_id = EXCLUDED.target_id,
                    action_type = EXCLUDED.action_type,
                    actor = EXCLUDED.actor,
                    comment = EXCLUDED.comment,
                    revised_payload = EXCLUDED.revised_payload
                """,
                (
                    action.id,
                    action.task_id,
                    action.target_type,
                    action.target_id,
                    action.action_type,
                    action.actor,
                    action.comment,
                    Jsonb(action.revised_payload),
                    action.created_at,
                ),
            )

        if task.report_snapshot is not None:
            snapshot = task.report_snapshot
            cur.execute(
                """
                INSERT INTO report_snapshot (
                    task_id, version, title, summary, recommendation, report_type, generated_by, rule_version,
                    source_file_sha256, file_path, file_sha256, payload, generated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, version) DO UPDATE SET
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    recommendation = EXCLUDED.recommendation,
                    report_type = EXCLUDED.report_type,
                    generated_by = EXCLUDED.generated_by,
                    rule_version = EXCLUDED.rule_version,
                    source_file_sha256 = EXCLUDED.source_file_sha256,
                    file_path = EXCLUDED.file_path,
                    file_sha256 = EXCLUDED.file_sha256,
                    payload = EXCLUDED.payload,
                    generated_at = EXCLUDED.generated_at
                """,
                (
                    task.id,
                    snapshot.version,
                    snapshot.title,
                    snapshot.summary,
                    snapshot.recommendation,
                    snapshot.report_type,
                    snapshot.generated_by,
                    snapshot.rule_version,
                    snapshot.source_file_sha256,
                    snapshot.file_path,
                    snapshot.file_sha256,
                    Jsonb(snapshot.model_dump()),
                    snapshot.generated_at,
                ),
            )

    def _backfill_child_tables(self, cur) -> None:  # noqa: ANN001
        cur.execute("SELECT count(*) FROM document_clause")
        if int(cur.fetchone()[0]) > 0:
            return
        cur.execute("SELECT payload FROM review_task ORDER BY created_at")
        for row in cur.fetchall():
            task = TaskRecord.model_validate(row[0])
            self._sync_task_children(cur, task)


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
                detail="未配置 DATABASE_URL。",
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
