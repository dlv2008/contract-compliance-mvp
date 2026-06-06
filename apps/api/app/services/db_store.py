from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings, get_settings
from app.models import (
    AssetSourceDocument,
    ConfigAsset,
    DatabaseProbe,
    LLMExecutionRecord,
    ReviewProfile,
    StepRunRecord,
    TaskRecord,
    WorkflowRunRecord,
)


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

CREATE TABLE IF NOT EXISTS config_asset (
    id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    applicability JSONB NOT NULL DEFAULT '{}'::jsonb,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version TEXT NOT NULL DEFAULT 'asset-v1',
    created_by TEXT NOT NULL DEFAULT 'system',
    approved_by TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    effective_from TIMESTAMPTZ,
    effective_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_config_asset_type_status ON config_asset (asset_type, status);
CREATE INDEX IF NOT EXISTS idx_config_asset_applicability ON config_asset USING GIN (applicability);

CREATE TABLE IF NOT EXISTS review_profile (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    applicability JSONB NOT NULL DEFAULT '{}'::jsonb,
    description TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_profile_status ON review_profile (status);

CREATE TABLE IF NOT EXISTS review_profile_asset (
    id BIGSERIAL PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES review_profile(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES config_asset(id),
    asset_type TEXT NOT NULL,
    asset_version INTEGER NOT NULL DEFAULT 1,
    required BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (profile_id, asset_id, asset_version)
);

CREATE TABLE IF NOT EXISTS llm_execution (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES review_task(id) ON DELETE SET NULL,
    purpose TEXT NOT NULL,
    asset_id TEXT,
    prompt_template_id TEXT,
    model TEXT NOT NULL,
    input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_output_preview TEXT,
    status TEXT NOT NULL,
    confidence DOUBLE PRECISION,
    latency_ms DOUBLE PRECISION,
    error_detail TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS asset_source_document (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'policy_document',
    name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL DEFAULT 'reviewer',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_asset_source_document_hash ON asset_source_document (content_hash);

CREATE TABLE IF NOT EXISTS workflow_run (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_type TEXT NOT NULL DEFAULT 'contract_review',
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'analyze_contract',
    input_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_run_task_id ON workflow_run (task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_run_status ON workflow_run (status);

CREATE TABLE IF NOT EXISTS workflow_step_run (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT,
    output_summary TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_run_task_id ON workflow_step_run (task_id, step_key);
CREATE INDEX IF NOT EXISTS idx_workflow_step_run_workflow ON workflow_step_run (workflow_run_id);

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
ALTER TABLE review_task ADD COLUMN IF NOT EXISTS selected_profile_id TEXT;
ALTER TABLE review_task ADD COLUMN IF NOT EXISTS selected_profile_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE config_asset ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE config_asset ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE config_asset ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE config_asset ADD COLUMN IF NOT EXISTS parent_asset_id TEXT;
ALTER TABLE config_asset ADD COLUMN IF NOT EXISTS approval_comment TEXT;
ALTER TABLE config_asset ADD COLUMN IF NOT EXISTS rejection_comment TEXT;
ALTER TABLE review_profile ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE review_profile ADD COLUMN IF NOT EXISTS parent_profile_id TEXT;
ALTER TABLE review_profile ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system';
ALTER TABLE review_profile ADD COLUMN IF NOT EXISTS published_by TEXT;
ALTER TABLE review_profile ADD COLUMN IF NOT EXISTS publish_comment TEXT;
ALTER TABLE review_profile_asset ADD COLUMN IF NOT EXISTS binding_reason TEXT;
ALTER TABLE llm_execution ADD COLUMN IF NOT EXISTS error_detail TEXT;
ALTER TABLE llm_execution ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
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
                        id, name, status, overall_risk, decision, source_filename, created_at,
                        selected_profile_id, selected_profile_snapshot, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        status = EXCLUDED.status,
                        overall_risk = EXCLUDED.overall_risk,
                        decision = EXCLUDED.decision,
                        source_filename = EXCLUDED.source_filename,
                        created_at = EXCLUDED.created_at,
                        selected_profile_id = EXCLUDED.selected_profile_id,
                        selected_profile_snapshot = EXCLUDED.selected_profile_snapshot,
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
                        task.selected_profile_id,
                        Jsonb(task.selected_profile_snapshot),
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
                    task_id, clause_id, title, text, status, sequence_no, parser_source, chunk_id, positions, version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, clause_id, version) DO UPDATE SET
                    title = EXCLUDED.title,
                    text = EXCLUDED.text,
                    status = EXCLUDED.status,
                    sequence_no = EXCLUDED.sequence_no,
                    parser_source = EXCLUDED.parser_source,
                    chunk_id = EXCLUDED.chunk_id,
                    positions = EXCLUDED.positions,
                    updated_at = now()
                """,
                (
                    task.id,
                    clause.id,
                    clause.title,
                    clause.text,
                    clause.status,
                    index,
                    clause.parser_source,
                    clause.parser_template_id,
                    Jsonb({}),
                    1,
                ),
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


class PostgresAssetStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.database_url:
            raise PostgresUnavailableError("未配置 DATABASE_URL。")
        self.ensure_schema()

    def load_state(self) -> tuple[list[ConfigAsset], list[ReviewProfile]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM config_asset ORDER BY asset_type, status, name, version DESC")
                assets = [ConfigAsset.model_validate(row[0]) for row in cur.fetchall()]
                cur.execute("SELECT payload FROM review_profile ORDER BY status, name, version DESC")
                profiles = [ReviewProfile.model_validate(row[0]) for row in cur.fetchall()]
        return assets, profiles

    def save_state(self, assets: list[ConfigAsset], profiles: list[ReviewProfile]) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM review_profile_asset")
                cur.execute("DELETE FROM review_profile")
                cur.execute("DELETE FROM config_asset")
                for asset in assets:
                    cur.execute(
                        """
                        INSERT INTO config_asset (
                            id, asset_type, name, version, status, applicability, content,
                            schema_version, created_by, approved_by, effective_from, effective_to,
                            content_hash, description, parent_asset_id, approval_comment,
                            rejection_comment, created_at, updated_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            asset.id,
                            asset.asset_type,
                            asset.name,
                            asset.version,
                            asset.status,
                            Jsonb(asset.applicability),
                            Jsonb(asset.content),
                            asset.schema_version,
                            asset.created_by,
                            asset.approved_by,
                            asset.effective_from,
                            asset.effective_to,
                            asset.content_hash,
                            asset.description,
                            asset.parent_asset_id,
                            asset.approval_comment,
                            asset.rejection_comment,
                            asset.created_at,
                            asset.updated_at,
                            Jsonb(asset.model_dump()),
                        ),
                    )
                for profile in profiles:
                    cur.execute(
                        """
                        INSERT INTO review_profile (
                            id, name, version, status, applicability, description, parent_profile_id,
                            created_by, published_by, publish_comment, created_at, updated_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            profile.id,
                            profile.name,
                            profile.version,
                            profile.status,
                            Jsonb(profile.applicability),
                            profile.description,
                            profile.parent_profile_id,
                            profile.created_by,
                            profile.published_by,
                            profile.publish_comment,
                            profile.created_at,
                            profile.updated_at,
                            Jsonb(profile.model_dump()),
                        ),
                    )
                    for ref in profile.assets:
                        cur.execute(
                            """
                            INSERT INTO review_profile_asset (
                                profile_id, asset_id, asset_type, asset_version, required, binding_reason
                            )
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                profile.id,
                                ref.asset_id,
                                ref.asset_type,
                                ref.asset_version,
                                ref.required,
                                ref.binding_reason,
                            ),
                        )
            conn.commit()

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def count_assets(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM config_asset")
                return int(cur.fetchone()[0])

    def count_profiles(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM review_profile")
                return int(cur.fetchone()[0])

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresUnavailableError("缺少 psycopg 依赖，无法连接 PostgreSQL。") from exc
        return psycopg.connect(self.settings.database_url, connect_timeout=5)


class PostgresAssetSourceDocumentStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.database_url:
            raise PostgresUnavailableError("未配置 DATABASE_URL。")
        PostgresAssetStore(self.settings).ensure_schema()

    def load_documents(self) -> list[AssetSourceDocument]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM asset_source_document ORDER BY created_at DESC")
                return [AssetSourceDocument.model_validate(row[0]) for row in cur.fetchall()]

    def save_documents(self, documents: list[AssetSourceDocument]) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM asset_source_document")
                for document in documents:
                    cur.execute(
                        """
                        INSERT INTO asset_source_document (
                            id, source_type, name, content_hash, payload, created_by, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            document.id,
                            document.source_type,
                            document.name,
                            document.content_hash,
                            Jsonb(document.model_dump()),
                            document.created_by,
                            document.created_at,
                            document.updated_at,
                        ),
                    )
            conn.commit()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresUnavailableError("缺少 psycopg 依赖，无法连接 PostgreSQL。") from exc
        return psycopg.connect(self.settings.database_url, connect_timeout=5)


class PostgresLLMExecutionStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.database_url:
            raise PostgresUnavailableError("未配置 DATABASE_URL。")
        PostgresAssetStore(self.settings).ensure_schema()

    def load_executions(self) -> list[LLMExecutionRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM llm_execution ORDER BY created_at DESC")
                return [LLMExecutionRecord.model_validate(row[0]) for row in cur.fetchall()]

    def save_executions(self, executions: list[LLMExecutionRecord]) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM llm_execution WHERE task_id IS NULL")
                for execution in executions:
                    cur.execute(
                        """
                        INSERT INTO llm_execution (
                            id, task_id, purpose, asset_id, prompt_template_id, model,
                            input_payload, output_payload, raw_output_preview, status,
                            confidence, latency_ms, error_detail, payload, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            output_payload = EXCLUDED.output_payload,
                            raw_output_preview = EXCLUDED.raw_output_preview,
                            status = EXCLUDED.status,
                            confidence = EXCLUDED.confidence,
                            latency_ms = EXCLUDED.latency_ms,
                            error_detail = EXCLUDED.error_detail,
                            payload = EXCLUDED.payload
                        """,
                        (
                            execution.id,
                            execution.task_id,
                            execution.purpose,
                            execution.asset_id,
                            execution.prompt_template_id,
                            execution.model,
                            Jsonb(execution.input_payload),
                            Jsonb(execution.output_payload),
                            execution.raw_output_preview,
                            execution.status,
                            execution.confidence,
                            execution.latency_ms,
                            execution.error_detail,
                            Jsonb(execution.model_dump()),
                            execution.created_at,
                        ),
                    )
            conn.commit()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresUnavailableError("缺少 psycopg 依赖，无法连接 PostgreSQL。") from exc
        return psycopg.connect(self.settings.database_url, connect_timeout=5)


class PostgresWorkflowRunStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.database_url:
            raise PostgresUnavailableError("未配置 DATABASE_URL。")
        self.ensure_schema()

    def load_runs(self) -> list[WorkflowRunRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM workflow_run ORDER BY started_at DESC")
                return [WorkflowRunRecord.model_validate(row[0]) for row in cur.fetchall()]

    def save_runs(self, runs: list[WorkflowRunRecord]) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM workflow_step_run")
                cur.execute("DELETE FROM workflow_run")
                for run in runs:
                    cur.execute(
                        """
                        INSERT INTO workflow_run (
                            id, task_id, run_type, status, source, input_hash, started_at,
                            finished_at, updated_at, retry_count, error, metadata, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run.id,
                            run.task_id,
                            run.run_type,
                            run.status,
                            run.source,
                            run.input_hash,
                            run.started_at,
                            run.finished_at,
                            run.updated_at,
                            run.retry_count,
                            run.error,
                            Jsonb(run.metadata),
                            Jsonb(run.model_dump()),
                        ),
                    )
                    for step in run.step_runs:
                        self._insert_step_run(cur, step)
            conn.commit()

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def count_runs(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM workflow_run")
                return int(cur.fetchone()[0])

    def _insert_step_run(self, cur, step: StepRunRecord) -> None:  # noqa: ANN001
        from psycopg.types.json import Jsonb

        cur.execute(
            """
            INSERT INTO workflow_step_run (
                id, workflow_run_id, task_id, step_key, label, status, input_hash,
                output_summary, error, retry_count, started_at, finished_at,
                updated_at, metadata, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                step.id,
                step.workflow_run_id,
                step.task_id,
                step.step_key,
                step.label,
                step.status,
                step.input_hash,
                step.output_summary,
                step.error,
                step.retry_count,
                step.started_at,
                step.finished_at,
                step.updated_at,
                Jsonb(step.metadata),
                Jsonb(step.model_dump()),
            ),
        )

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
