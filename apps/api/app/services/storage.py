from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import AgentTraceEvent, ReportSnapshot, ReviewActionRecord, TaskRecord
from app.services.assets import AssetNotFoundError, AssetRegistry
from app.services.db_store import PostgresTaskStore
from app.services.object_store import ObjectStorageError, ObjectStore
from app.services.review_engine import (
    DECISION_LABELS,
    OVERALL_RISK_LABELS,
    STATUS_LABELS,
    analyze_contract,
    build_report_snapshot,
    build_summary,
    build_workflow_steps,
    derive_decision,
    derive_overall_risk,
    derive_status,
)


SUPPORTED_TEXT_EXTENSIONS = {".md", ".txt", ".text"}
REVIEW_ACTION_TYPES = {"confirm", "reject", "rewrite_suggestion", "request_evidence"}
REVIEW_STATUS_BY_ACTION = {
    "confirm": "confirmed",
    "reject": "rejected",
    "rewrite_suggestion": "revised",
    "request_evidence": "evidence_requested",
}
TASK_DECISION_ACTION_TYPES = {"approve", "return_materials", "require_revision", "archive"}
TASK_STATUS_BY_DECISION_ACTION = {
    "approve": "final_approved",
    "return_materials": "returned_for_materials",
    "require_revision": "revision_required",
    "archive": "archived",
}
TASK_DECISION_BY_ACTION = {
    "approve": "final_approved",
    "return_materials": "returned_for_materials",
    "require_revision": "revision_required",
    "archive": "archived",
}


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
        selected_profile_id: str | None = None,
        require_profile: bool = True,
    ) -> TaskRecord:
        selected_profile = self._resolve_profile(selected_profile_id, require_profile=require_profile)
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
            rule_context=AssetRegistry(self.settings).rule_context_for_profile(selected_profile),
        )
        task = self._apply_profile_snapshot(task, selected_profile).model_copy(update={"stored_file": stored_file})
        task = self._write_report_snapshot(task)
        if self._use_postgres():
            self._bootstrap_if_needed()
            self._postgres_store().upsert_task(task, event_message="uploaded contract and generated review snapshot")
        else:
            tasks = self.list_tasks()
            tasks.insert(0, task)
            self._save_tasks(tasks)
        return task

    def list_document_clauses(self, task_id: str) -> list[dict]:
        if self._use_postgres():
            self._bootstrap_if_needed()
            return self._postgres_store().list_document_clauses(task_id)
        task = self.get_task(task_id)
        if task is None:
            return []
        return [
            {
                "task_id": task.id,
                "clause_id": clause.id,
                "title": clause.title,
                "text": clause.text,
                "status": clause.status,
                "sequence_no": index,
                "parser_source": "local",
                "chunk_id": None,
                "page_start": None,
                "page_end": None,
                "positions": {},
                "version": 1,
            }
            for index, clause in enumerate(task.clauses, start=1)
        ]

    def list_extracted_facts(self, task_id: str) -> list[dict]:
        if self._use_postgres():
            self._bootstrap_if_needed()
            return self._postgres_store().list_extracted_facts(task_id)
        task = self.get_task(task_id)
        if task is None:
            return []
        return [
            {
                "task_id": task.id,
                "fact_key": field.key,
                "label": field.label,
                "value": field.value,
                "status": field.status,
                "evidence_clause_ids": field.evidence_clause_ids,
                "extractor": "deterministic-mvp",
                "schema_version": "mvp-facts-v1",
            }
            for field in task.extracted_fields
        ]

    def list_rule_hits(self, task_id: str) -> list[dict]:
        if self._use_postgres():
            self._bootstrap_if_needed()
            return self._postgres_store().list_rule_hits(task_id)
        task = self.get_task(task_id)
        if task is None:
            return []
        return [
            {
                "task_id": task.id,
                "rule_id": risk.rule_id,
                "rule_version": risk.rule_version,
                "title": risk.title,
                "level": risk.level,
                "message": risk.message,
                "reason": risk.reason,
                "evidence_clause_ids": risk.evidence_clause_ids,
                "policy_reference_ids": risk.policy_reference_ids,
                "action": risk.action,
                "engine": "deterministic",
                "review_status": risk.review_status,
                "reviewer_comment": risk.reviewer_comment,
            }
            for risk in task.risks
        ]

    def list_review_actions(self, task_id: str) -> list[dict]:
        if self._use_postgres():
            self._bootstrap_if_needed()
            return self._postgres_store().list_review_actions(task_id)
        task = self.get_task(task_id)
        if task is None:
            return []
        return [action.model_dump() for action in sorted(task.review_actions, key=lambda item: item.created_at, reverse=True)]

    def list_report_snapshots(self, task_id: str) -> list[dict]:
        if self._use_postgres():
            self._bootstrap_if_needed()
            return self._postgres_store().list_report_snapshots(task_id)
        task = self.get_task(task_id)
        if task is None or task.report_snapshot is None:
            return []
        return [{"task_id": task.id, **task.report_snapshot.model_dump()}]

    def record_review_action(
        self,
        task_id: str,
        *,
        target_type: str,
        target_id: str,
        action_type: str,
        actor: str = "reviewer",
        comment: str | None = None,
        revised_payload: dict | None = None,
    ) -> ReviewActionRecord:
        if action_type not in REVIEW_ACTION_TYPES:
            supported = ", ".join(sorted(REVIEW_ACTION_TYPES))
            raise ContractUploadError(f"Unsupported review action: {action_type}. Supported: {supported}.")

        task = self.get_task(task_id)
        if task is None:
            raise ContractUploadError("Task does not exist.")

        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        action = ReviewActionRecord(
            id=f"review-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            target_type=target_type,
            target_id=target_id,
            action_type=action_type,
            actor=actor or "reviewer",
            comment=comment,
            revised_payload=revised_payload or {},
            created_at=now,
        )
        updated_task = self._apply_review_action(task, action)
        updated_task = self._write_report_snapshot(updated_task)
        self._persist_task(updated_task, "recorded manual review action")
        return action

    def record_task_decision(
        self,
        task_id: str,
        *,
        action_type: str,
        actor: str = "reviewer",
        comment: str | None = None,
    ) -> ReviewActionRecord:
        if action_type not in TASK_DECISION_ACTION_TYPES:
            supported = ", ".join(sorted(TASK_DECISION_ACTION_TYPES))
            raise ContractUploadError(f"Unsupported task decision: {action_type}. Supported: {supported}.")

        task = self.get_task(task_id)
        if task is None:
            raise ContractUploadError("Task does not exist.")

        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        action = ReviewActionRecord(
            id=f"review-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            target_type="task",
            target_id=task_id,
            action_type=action_type,
            actor=actor or "reviewer",
            comment=comment,
            revised_payload={},
            created_at=now,
        )
        updated_task = self._apply_task_decision(task, action)
        updated_task = self._write_report_snapshot(updated_task)
        self._persist_task(updated_task, "recorded task-level review decision")
        return action

    def generate_delivery_report(
        self,
        task_id: str,
        *,
        actor: str = "reviewer",
        comment: str | None = None,
    ) -> ReportSnapshot:
        task = self.get_task(task_id)
        if task is None:
            raise ContractUploadError("Task does not exist.")

        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        action = ReviewActionRecord(
            id=f"review-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            target_type="report",
            target_id=task_id,
            action_type="generate_delivery_report",
            actor=actor or "reviewer",
            comment=comment,
            revised_payload={},
            created_at=now,
        )
        active_risks = [risk for risk in task.risks if risk.review_status != "rejected"]
        previous_version = task.report_snapshot.version if task.report_snapshot else 0
        report_snapshot = build_report_snapshot(
            task.name,
            active_risks,
            task.decision,
            report_type="delivery_report",
            generated_by=actor or "reviewer",
        ).model_copy(update={"version": previous_version + 1})
        trace = list(task.agent_trace)
        trace.append(
            AgentTraceEvent(
                at=action.created_at,
                type="report.generate",
                message="Delivery report generated explicitly.",
                payload=action.model_dump(),
            )
        )
        updated_task = task.model_copy(
            update={
                "review_actions": [*task.review_actions, action],
                "agent_trace": trace,
                "report_snapshot": report_snapshot,
            }
        )
        updated_task = self._write_report_snapshot(updated_task)
        self._persist_task(updated_task, "generated delivery report")
        if updated_task.report_snapshot is None:
            raise TaskStorageError("Report snapshot was not generated.")
        return updated_task.report_snapshot

    def read_report_markdown(self, task_id: str, version: int) -> str:
        task = self.get_task(task_id)
        if task is None:
            raise ContractUploadError("Task does not exist.")
        snapshots = self.list_report_snapshots(task_id)
        snapshot = next((item for item in snapshots if int(item["version"]) == version), None)
        if snapshot is None:
            raise ContractUploadError("Report version does not exist.")
        file_path = snapshot.get("file_path")
        if file_path and Path(file_path).exists():
            return Path(file_path).read_text(encoding="utf-8")
        if task.report_snapshot and task.report_snapshot.version == version:
            return render_report_markdown(task, task.report_snapshot)
        raise TaskStorageError("Report file is missing.")

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
            return [self._ensure_profile_snapshot(TaskRecord.model_validate(item)) for item in payload]
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
        self.settings.report_dir.mkdir(parents=True, exist_ok=True)

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
            initial_task = analyze_contract(
                task_id=f"sample-{index:03d}",
                source_filename=sample_path.name,
                contract_name=sample_path.stem,
                contract_text=payload.decode("utf-8"),
            )
            registry = AssetRegistry(self.settings)
            profile = registry.default_profile_for_contract_type(initial_task.contract_type)
            task = analyze_contract(
                task_id=f"sample-{index:03d}",
                source_filename=sample_path.name,
                contract_name=sample_path.stem,
                contract_text=payload.decode("utf-8"),
                rule_context=registry.rule_context_for_profile(profile),
            )
            task = self._apply_profile_snapshot(task, profile).model_copy(update={"stored_file": stored_file})
            tasks.append(task)
        return tasks

    def _resolve_profile(self, profile_id: str | None, *, require_profile: bool):
        registry = AssetRegistry(self.settings)
        try:
            return registry.get_active_profile(profile_id)
        except AssetNotFoundError as exc:
            if require_profile:
                raise ContractUploadError(str(exc)) from exc
            return registry.get_active_profile("profile-basic-contract-review-v1")

    def _apply_profile_snapshot(self, task: TaskRecord, profile) -> TaskRecord:  # noqa: ANN001
        registry = AssetRegistry(self.settings)
        return task.model_copy(
            update={
                "selected_profile_id": profile.id,
                "selected_profile_name": profile.name,
                "selected_profile_snapshot": registry.freeze_profile(profile),
            }
        )

    def _ensure_profile_snapshot(self, task: TaskRecord) -> TaskRecord:
        if task.selected_profile_id and task.selected_profile_snapshot:
            return task
        profile = AssetRegistry(self.settings).default_profile_for_contract_type(task.contract_type)
        return self._apply_profile_snapshot(task, profile)

    def _load_existing_json_tasks(self) -> list[TaskRecord]:
        if not self.settings.task_store_path.exists():
            return []
        return self._load_tasks()

    def _use_postgres(self) -> bool:
        return self.settings.task_store_backend == "postgres"

    def _postgres_store(self) -> PostgresTaskStore:
        return PostgresTaskStore(self.settings)

    def _persist_task(self, task: TaskRecord, event_message: str) -> None:
        if self._use_postgres():
            self._postgres_store().upsert_task(task, event_message=event_message)
            return
        tasks = [candidate for candidate in self.list_tasks() if candidate.id != task.id]
        tasks.insert(0, task)
        self._save_tasks(tasks)

    def _apply_review_action(self, task: TaskRecord, action: ReviewActionRecord) -> TaskRecord:
        review_status = REVIEW_STATUS_BY_ACTION[action.action_type]
        updated_risks = []
        for risk in task.risks:
            if action.target_type == "rule_hit" and risk.rule_id == action.target_id:
                updates = {
                    "review_status": review_status,
                    "reviewer_comment": action.comment,
                }
                if action.action_type == "rewrite_suggestion":
                    if "action" in action.revised_payload:
                        updates["action"] = str(action.revised_payload["action"])
                    if "reason" in action.revised_payload:
                        updates["reason"] = str(action.revised_payload["reason"])
                updated_risks.append(risk.model_copy(update=updates))
            else:
                updated_risks.append(risk)

        trace = list(task.agent_trace)
        trace.append(
            AgentTraceEvent(
                at=action.created_at,
                type="review.action",
                message=f"Manual review action recorded: {action.action_type} {action.target_type}:{action.target_id}",
                payload=action.model_dump(),
            )
        )
        reviewed_task = task.model_copy(
            update={
                "risks": updated_risks,
                "review_actions": [*task.review_actions, action],
                "agent_trace": trace,
            }
        )
        return self._refresh_review_outcome(reviewed_task)

    def _refresh_review_outcome(self, task: TaskRecord) -> TaskRecord:
        active_risks = [risk for risk in task.risks if risk.review_status != "rejected"]
        overall_risk = derive_overall_risk(active_risks)
        status = derive_status(overall_risk, active_risks)
        decision = derive_decision(overall_risk, active_risks)
        workflow_steps = build_workflow_steps(datetime.now(timezone.utc).isoformat(timespec="seconds"), status)

        previous_version = task.report_snapshot.version if task.report_snapshot else 0
        report_snapshot = build_report_snapshot(task.name, active_risks, decision).model_copy(
            update={"version": previous_version + 1}
        )
        return task.model_copy(
            update={
                "status": status,
                "status_label": status_label(status),
                "overall_risk": overall_risk,
                "overall_risk_label": overall_risk_label(overall_risk),
                "decision": decision,
                "decision_label": decision_label(decision),
                "summary": build_review_summary(active_risks),
                "workflow_steps": workflow_steps,
                "report_snapshot": report_snapshot,
            }
        )

    def _apply_task_decision(self, task: TaskRecord, action: ReviewActionRecord) -> TaskRecord:
        status = TASK_STATUS_BY_DECISION_ACTION[action.action_type]
        decision = TASK_DECISION_BY_ACTION[action.action_type]
        active_risks = [risk for risk in task.risks if risk.review_status != "rejected"]
        previous_version = task.report_snapshot.version if task.report_snapshot else 0
        report_snapshot = build_report_snapshot(task.name, active_risks, decision).model_copy(
            update={"version": previous_version + 1}
        )
        workflow_steps = build_workflow_steps(action.created_at, status)
        trace = list(task.agent_trace)
        trace.append(
            AgentTraceEvent(
                at=action.created_at,
                type="review.finalize",
                message=f"Task-level review decision recorded: {action.action_type}",
                payload=action.model_dump(),
            )
        )
        return task.model_copy(
            update={
                "status": status,
                "status_label": status_label(status),
                "decision": decision,
                "decision_label": decision_label(decision),
                "summary": build_task_decision_summary(active_risks, action.action_type),
                "review_actions": [*task.review_actions, action],
                "workflow_steps": workflow_steps,
                "agent_trace": trace,
                "report_snapshot": report_snapshot,
            }
        )

    def _write_report_snapshot(self, task: TaskRecord) -> TaskRecord:
        if task.report_snapshot is None:
            return task

        snapshot = task.report_snapshot
        version = snapshot.version or 1
        target_dir = self.settings.report_dir / task.id
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = "delivery" if snapshot.report_type == "delivery_report" else "snapshot"
        target_path = target_dir / f"report-v{version}-{suffix}.md"
        report_text = render_report_markdown(task, snapshot)
        target_path.write_text(report_text, encoding="utf-8")
        file_sha256 = hashlib.sha256(report_text.encode("utf-8")).hexdigest()
        source_hash = task.stored_file.sha256 if task.stored_file else snapshot.source_file_sha256
        updated_snapshot = snapshot.model_copy(
            update={
                "version": version,
                "source_file_sha256": source_hash,
                "file_path": str(target_path),
                "file_sha256": file_sha256,
            }
        )
        return task.model_copy(update={"report_snapshot": updated_snapshot})


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


def render_report_markdown(task: TaskRecord, snapshot: ReportSnapshot) -> str:
    lines = [
        f"# {snapshot.title}",
        "",
        f"- Task ID: {task.id}",
        f"- Source file: {task.source_filename}",
        f"- Original SHA256: {task.stored_file.sha256 if task.stored_file else 'unknown'}",
        f"- Rule version: {snapshot.rule_version}",
        f"- Report version: {snapshot.version}",
        f"- Report type: {snapshot.report_type_label} ({snapshot.report_type})",
        f"- Generated by: {snapshot.generated_by}",
        f"- Generated at: {snapshot.generated_at}",
        "",
        "## Summary",
        "",
        snapshot.summary,
        "",
        "## Recommendation",
        "",
        snapshot.recommendation,
        "",
        "## Rule hits",
        "",
    ]
    if not task.risks:
        lines.append("- No rule hits.")
    for risk in task.risks:
        evidence = ", ".join(risk.evidence_clause_ids) or "none"
        policy = ", ".join(risk.policy_reference_ids) or "none"
        lines.extend(
            [
                f"- {risk.rule_id} ({risk.level}, {risk.rule_version})",
                f"  - Title: {risk.title}",
                f"  - Evidence clauses: {evidence}",
                f"  - Policy references: {policy}",
                f"  - Review status: {risk.review_status}",
                f"  - Action: {risk.action}",
            ]
        )
    if task.review_actions:
        lines.extend(["", "## Review actions", ""])
        for action in task.review_actions:
            lines.extend(
                [
                    f"- {action.created_at} {action.action_type} ({action.target_type}:{action.target_id})",
                    f"  - Actor: {action.actor}",
                    f"  - Comment: {action.comment or 'none'}",
                ]
            )
    lines.append("")
    return "\n".join(lines)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def overall_risk_label(overall_risk: str) -> str:
    return OVERALL_RISK_LABELS.get(overall_risk, overall_risk)


def decision_label(decision: str) -> str:
    return DECISION_LABELS.get(decision, decision)


def build_review_summary(active_risks: list) -> str:
    return build_summary(active_risks)


def build_task_decision_summary(active_risks: list, action_type: str) -> str:
    if action_type == "approve":
        return "整单复核已提交通过，当前风险与例外处理意见已进入报告快照。"
    if action_type == "return_materials":
        return "整单已退回补充材料，待业务侧补齐审批、证明或说明后重新审查。"
    if action_type == "require_revision":
        return "整单已要求整改，待合同条款修改后重新提交复核。"
    if action_type == "archive":
        return "整单已归档，审查结论、复核动作和报告快照已留痕。"
    return build_summary(active_risks)
