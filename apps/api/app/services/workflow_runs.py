from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import StepRunRecord, TaskRecord, WorkflowRunRecord, WorkflowStep


class WorkflowRunError(RuntimeError):
    pass


class WorkflowRunStore(Protocol):
    def load_runs(self) -> list[WorkflowRunRecord]:
        pass

    def save_runs(self, runs: list[WorkflowRunRecord]) -> None:
        pass


class JsonWorkflowRunStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_runs(self) -> list[WorkflowRunRecord]:
        if not self.state_path.exists():
            self.save_runs([])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [WorkflowRunRecord.model_validate(item) for item in payload.get("workflow_runs", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise WorkflowRunError("Workflow run store is unreadable.") from exc

    def save_runs(self, runs: list[WorkflowRunRecord]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {"workflow_runs": [run.model_dump() for run in runs]}
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


class WorkflowRunRepository:
    def __init__(self, settings: Settings | None = None, store: WorkflowRunStore | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = store or self._default_store()

    def _default_store(self) -> WorkflowRunStore:
        if self.settings.workflow_store_backend == "postgres":
            from app.services.db_store import PostgresWorkflowRunStore

            return PostgresWorkflowRunStore(self.settings)
        return JsonWorkflowRunStore(self.settings.data_dir / "workflow_runs.json")

    def list_runs(self, task_id: str | None = None, *, limit: int = 50) -> list[WorkflowRunRecord]:
        runs = self.store.load_runs()
        if task_id:
            runs = [run for run in runs if run.task_id == task_id]
        return sorted(runs, key=lambda run: run.started_at, reverse=True)[:limit]

    def latest_for_task(self, task_id: str) -> WorkflowRunRecord | None:
        runs = self.list_runs(task_id, limit=1)
        return runs[0] if runs else None

    def record_from_task(
        self,
        task: TaskRecord,
        *,
        run_type: str = "contract_review",
        retry_step_key: str | None = None,
        previous_run: WorkflowRunRecord | None = None,
    ) -> WorkflowRunRecord:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        retry_counts = {
            step.step_key: step.retry_count
            for step in (previous_run.step_runs if previous_run else [])
        }
        if retry_step_key:
            retry_counts[retry_step_key] = retry_counts.get(retry_step_key, 0) + 1
        run = WorkflowRunRecord(
            id=f"workflow-{task.id}",
            task_id=task.id,
            run_type=run_type,
            status=_workflow_status(task.workflow_steps),
            source="analyze_contract.v1" if retry_step_key is None else "analyze_contract.v1.retry",
            input_hash=_task_input_hash(task),
            started_at=task.created_at,
            finished_at=now,
            updated_at=now,
            retry_count=sum(retry_counts.values()),
            step_runs=[
                _build_step_run(
                    workflow_run_id=f"workflow-{task.id}",
                    task=task,
                    step=step,
                    index=index,
                    run_type=run_type,
                    retry_count=retry_counts.get(step.key, 0),
                )
                for index, step in enumerate(task.workflow_steps, start=1)
            ],
            metadata={
                "contract_name": task.name,
                "contract_type": task.contract_type,
                "profile_id": task.selected_profile_id,
                "profile_name": task.selected_profile_name,
                "retry_step_key": retry_step_key,
            },
        )
        runs = [item for item in self.store.load_runs() if item.id != run.id]
        runs.append(run)
        self.store.save_runs(sorted(runs, key=lambda item: item.started_at, reverse=True)[:500])
        return run


def _workflow_status(steps: list[WorkflowStep]) -> str:
    statuses = {step.status for step in steps}
    if "failed" in statuses:
        return "failed"
    if "waiting" in statuses or "waiting_human" in statuses:
        return "waiting_human"
    if "warning" in statuses:
        return "succeeded_with_warnings"
    return "succeeded"


def _step_status(status: str) -> str:
    return {
        "done": "succeeded",
        "warning": "succeeded",
        "waiting": "waiting_human",
    }.get(status, status)


def _task_input_hash(task: TaskRecord) -> str:
    payload = {
        "contract_text": task.contract_text,
        "source_filename": task.source_filename,
        "selected_profile_snapshot": task.selected_profile_snapshot,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_step_run(
    *,
    workflow_run_id: str,
    task: TaskRecord,
    step: WorkflowStep,
    index: int,
    run_type: str,
    retry_count: int = 0,
) -> StepRunRecord:
    status = _step_status(step.status)
    summary = _step_summary(task, step.key)
    return StepRunRecord(
        id=f"{workflow_run_id}-{index:02d}-{step.key}",
        workflow_run_id=workflow_run_id,
        task_id=task.id,
        step_key=step.key,
        label=step.label,
        status=status,
        input_hash=_task_input_hash(task),
        output_summary=summary,
        error=_step_error(task, step.key, step.status),
        retry_count=retry_count,
        started_at=step.updated_at or task.created_at,
        finished_at=step.updated_at if status in {"succeeded", "failed"} else None,
        updated_at=step.updated_at or task.created_at,
        metadata={
            "legacy_status": step.status,
            "run_type": run_type,
        },
    )


def _step_summary(task: TaskRecord, step_key: str) -> str:
    if step_key == "uploaded":
        return f"Source file {task.source_filename} archived for review."
    if step_key == "parsing":
        fallback_used = any(clause.parser_source.startswith("fallback:") for clause in task.clauses)
        return f"Parsed {len(task.clauses)} clauses; fallback_used={fallback_used}."
    if step_key == "extracting":
        candidate_count = sum(1 for field in task.extracted_fields if field.status == "candidate")
        return f"Extracted {len(task.extracted_fields)} fields; llm_candidates={candidate_count}."
    if step_key == "evaluating":
        return f"Evaluated hard and deterministic rules; risks={len(task.risks)}."
    if step_key == "semantic_rules":
        semantic_event = next((event for event in task.agent_trace if event.type == "semantic.evaluate"), None)
        if semantic_event:
            return semantic_event.message
        return "No semantic rules were configured for this profile."
    if step_key == "review":
        return f"Routed to task status {task.status}."
    if step_key == "report":
        return f"Generated report snapshot v{task.report_snapshot.version if task.report_snapshot else 0}."
    return step_key


def _step_error(task: TaskRecord, step_key: str, status: str) -> str | None:
    if status not in {"warning", "failed"}:
        return None
    if step_key == "parsing":
        return "Clause parser used fallback parsing."
    if step_key == "semantic_rules":
        semantic_event = next((event for event in task.agent_trace if event.type == "semantic.evaluate"), None)
        warning_count = int((semantic_event.payload if semantic_event else {}).get("warning_count") or 0)
        return f"Semantic rule warnings: {warning_count}."
    return f"Step completed with status {status}."
