from __future__ import annotations

import hashlib
import json
import threading
import time
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
    _io_lock = threading.Lock()

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_runs(self) -> list[WorkflowRunRecord]:
        with self._io_lock:
            if not self.state_path.exists():
                self._save_runs_unlocked([])
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                return [WorkflowRunRecord.model_validate(item) for item in payload.get("workflow_runs", [])]
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                raise WorkflowRunError("Workflow run store is unreadable.") from exc

    def save_runs(self, runs: list[WorkflowRunRecord]) -> None:
        with self._io_lock:
            self._save_runs_unlocked(runs)

    def _save_runs_unlocked(self, runs: list[WorkflowRunRecord]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(f".{threading.get_ident()}.tmp")
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
        source: str | None = None,
        resume_from_step: str | None = None,
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
            source=source
            or ("analyze_contract.v2.executor" if retry_step_key is None else "analyze_contract.v2.executor.retry"),
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
                "resume_from_step": resume_from_step,
                "checkpoint_count": len(_checkpoint_payloads(task)),
                "checkpoints": _checkpoint_payloads(task),
                "reused_checkpoint_count": len(_reused_checkpoint_payloads(task)),
                "reused_checkpoint_steps": [
                    item.get("step_key")
                    for item in _reused_checkpoint_payloads(task)
                    if item.get("step_key")
                ],
                "execution_plan": _execution_plan(task),
                "previous_workflow_run_id": previous_run.id if previous_run else None,
            },
        )
        run = _apply_inline_worker_plan(run)
        runs = [item for item in self.store.load_runs() if item.id != run.id]
        runs.append(run)
        self.store.save_runs(sorted(runs, key=lambda item: item.started_at, reverse=True)[:500])
        return run

    def status_payload(self, task_id: str) -> dict | None:
        run = self.latest_for_task(task_id)
        if run is None:
            return None
        plan = run.metadata.get("execution_plan") or []
        worker_results = run.metadata.get("worker_results") or []
        total = len(plan)
        completed = sum(
            1
            for item in worker_results
            if item.get("worker_status") in {"executed_inline", "executed_by_background_worker", "skipped_by_checkpoint"}
        )
        return {
            "workflow_run_id": run.id,
            "task_id": run.task_id,
            "status": run.status,
            "source": run.source,
            "worker_mode": run.metadata.get("worker_mode"),
            "worker_status": run.metadata.get("worker_status"),
            "progress": {
                "total_steps": total,
                "completed_steps": completed,
                "percent": round((completed / total) * 100, 2) if total else 0,
            },
            "resume_from_step": run.metadata.get("resume_from_step"),
            "reused_checkpoint_steps": run.metadata.get("reused_checkpoint_steps", []),
            "execution_plan": plan,
            "worker_results": worker_results,
            "updated_at": run.updated_at,
        }

    def queue_worker_run(self, task_id: str) -> WorkflowRunRecord:
        run = self.latest_for_task(task_id)
        if run is None:
            raise WorkflowRunError("Workflow run does not exist.")
        queued_run = _apply_worker_state(run, "queued")
        self._replace_run(queued_run)
        return queued_run

    def transition_worker(self, task_id: str, action: str) -> WorkflowRunRecord:
        run = self.latest_for_task(task_id)
        if run is None:
            raise WorkflowRunError("Workflow run does not exist.")
        if action not in {"start", "pause", "resume", "complete"}:
            raise WorkflowRunError("Unsupported workflow worker action.")
        current = run.metadata.get("worker_status")
        if action == "start" and current not in {"queued", "paused", "completed"}:
            raise WorkflowRunError("Workflow worker can only start from queued, paused or completed state.")
        if action == "pause" and current not in {"queued", "running"}:
            raise WorkflowRunError("Workflow worker can only pause from queued or running state.")
        if action == "resume" and current == "completed":
            return run
        if action == "complete" and current not in {"queued", "running", "paused"}:
            raise WorkflowRunError("Workflow worker can only complete from queued, running or paused state.")
        target_status = {
            "start": "running",
            "pause": "paused",
            "resume": "running",
            "complete": "completed",
        }[action]
        updated = _apply_worker_state(run, target_status)
        self._replace_run(updated)
        if action in {"start", "resume"}:
            WorkflowBackgroundWorker.start(updated.task_id, self.settings)
        return updated

    def mark_worker_step(self, task_id: str, step_key: str, worker_status: str) -> WorkflowRunRecord:
        run = self.latest_for_task(task_id)
        if run is None:
            raise WorkflowRunError("Workflow run does not exist.")
        updated = _apply_worker_step_result(run, step_key, worker_status)
        self._replace_run(updated)
        return updated

    def complete_background_worker(self, task_id: str) -> WorkflowRunRecord:
        run = self.latest_for_task(task_id)
        if run is None:
            raise WorkflowRunError("Workflow run does not exist.")
        updated = _apply_worker_state(run, "completed")
        self._replace_run(updated)
        return updated

    def _replace_run(self, run: WorkflowRunRecord) -> None:
        runs = [item for item in self.store.load_runs() if item.id != run.id]
        runs.append(run)
        self.store.save_runs(sorted(runs, key=lambda item: item.started_at, reverse=True)[:500])


class WorkflowBackgroundWorker:
    _lock = threading.Lock()
    _active: set[str] = set()

    @classmethod
    def start(cls, task_id: str, settings: Settings) -> None:
        with cls._lock:
            if task_id in cls._active:
                return
            cls._active.add(task_id)
        thread = threading.Thread(target=cls._run, args=(task_id, settings), daemon=True)
        thread.start()

    @classmethod
    def _run(cls, task_id: str, settings: Settings) -> None:
        repo = WorkflowRunRepository(settings=settings)
        try:
            while True:
                run = repo.latest_for_task(task_id)
                if run is None:
                    return
                if run.metadata.get("worker_status") != "running":
                    return
                next_step = _next_pending_worker_step(run)
                if next_step is None:
                    repo.complete_background_worker(task_id)
                    return
                step_key = next_step["step_key"]
                action = next_step.get("action") or "execute"
                if action == "reuse_checkpoint":
                    repo.mark_worker_step(task_id, step_key, "skipped_by_checkpoint")
                    continue
                repo.mark_worker_step(task_id, step_key, "running")
                time.sleep(0.05)
                latest = repo.latest_for_task(task_id)
                if latest is None or latest.metadata.get("worker_status") != "running":
                    return
                repo.mark_worker_step(task_id, step_key, "executed_by_background_worker")
        except Exception as exc:  # pragma: no cover - defensive guard for daemon worker
            try:
                run = repo.latest_for_task(task_id)
                if run is not None:
                    failed = _apply_worker_failure(run, str(exc))
                    repo._replace_run(failed)
            finally:
                return
        finally:
            with cls._lock:
                cls._active.discard(task_id)


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
    checkpoint = _checkpoint_for_step(task, step.key)
    summary = checkpoint.get("output_summary") if checkpoint else _step_summary(task, step.key)
    checkpoint_status = checkpoint.get("status") if checkpoint else None
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
            "checkpoint_status": checkpoint_status,
            "checkpoint_saved": bool(checkpoint),
            "resume_mode": checkpoint.get("resume_mode") if checkpoint else None,
            "resume_from_step": checkpoint.get("resume_from_step") if checkpoint else None,
            "execution_mode": checkpoint.get("execution_mode") if checkpoint else None,
            "reused_checkpoint": bool(checkpoint.get("reused_checkpoint")) if checkpoint else False,
        },
    )


def _checkpoint_payloads(task: TaskRecord) -> list[dict]:
    return [
        event.payload
        for event in task.agent_trace
        if event.type == "workflow.checkpoint" and isinstance(event.payload, dict)
    ]


def _reused_checkpoint_payloads(task: TaskRecord) -> list[dict]:
    return [payload for payload in _checkpoint_payloads(task) if payload.get("reused_checkpoint")]


def _execution_plan(task: TaskRecord) -> list[dict]:
    checkpoints = {payload.get("step_key"): payload for payload in _checkpoint_payloads(task)}
    plan = []
    for step in task.workflow_steps:
        checkpoint = checkpoints.get(step.key) or {}
        reused = bool(checkpoint.get("reused_checkpoint"))
        plan.append(
            {
                "step_key": step.key,
                "label": step.label,
                "action": "reuse_checkpoint" if reused else "execute",
                "status": "skipped" if reused else _step_status(step.status),
                "reason": (
                    "Existing checkpoint is before resume_from_step and can be reused."
                    if reused
                    else "Step is at or after resume_from_step, or this is a fresh execution."
                ),
                "resume_from_step": checkpoint.get("resume_from_step"),
                "checkpoint_status": checkpoint.get("status"),
            }
        )
    return plan


def _apply_inline_worker_plan(run: WorkflowRunRecord) -> WorkflowRunRecord:
    plan_by_step = {
        item.get("step_key"): item
        for item in (run.metadata.get("execution_plan") or [])
        if item.get("step_key")
    }
    worker_results = []
    updated_steps = []
    for step in run.step_runs:
        plan = plan_by_step.get(step.step_key) or {}
        action = plan.get("action") or "execute"
        if action == "reuse_checkpoint":
            worker_status = "skipped_by_checkpoint"
            step_status = "skipped"
        else:
            worker_status = "executed_inline"
            step_status = step.status
        worker_results.append(
            {
                "step_key": step.step_key,
                "action": action,
                "worker_status": worker_status,
                "status": step_status,
                "reason": plan.get("reason"),
            }
        )
        metadata = dict(step.metadata)
        metadata.update(
            {
                "worker_action": action,
                "worker_status": worker_status,
                "physical_skip": action == "reuse_checkpoint",
            }
        )
        updated_steps.append(step.model_copy(update={"status": step_status, "metadata": metadata}))
    metadata = dict(run.metadata)
    metadata.update(
        {
            "worker_mode": "inline_plan_executor",
            "worker_status": "completed",
            "worker_results": worker_results,
        }
    )
    return run.model_copy(update={"step_runs": updated_steps, "metadata": metadata})


def _apply_worker_state(run: WorkflowRunRecord, worker_status: str) -> WorkflowRunRecord:
    plan_by_step = {
        item.get("step_key"): item
        for item in (run.metadata.get("execution_plan") or [])
        if item.get("step_key")
    }
    existing_results = _worker_results_by_step(run)
    first_pending_seen = False
    worker_results = []
    updated_steps = []
    for step in run.step_runs:
        plan = plan_by_step.get(step.step_key) or {}
        action = plan.get("action") or step.metadata.get("worker_action") or "execute"
        metadata = dict(step.metadata)
        previous_status = (existing_results.get(step.step_key) or {}).get("worker_status")
        if worker_status == "completed":
            if previous_status in {"skipped_by_checkpoint", "executed_inline", "executed_by_background_worker"}:
                result_status = previous_status
            else:
                result_status = "skipped_by_checkpoint" if action == "reuse_checkpoint" else "executed_inline"
            step_status = "skipped" if action == "reuse_checkpoint" else _step_status(metadata.get("legacy_status") or step.status)
            physical_skip = action == "reuse_checkpoint"
        elif worker_status == "running":
            if previous_status in {"skipped_by_checkpoint", "executed_inline", "executed_by_background_worker"}:
                result_status = previous_status
                step_status = "skipped" if action == "reuse_checkpoint" else _step_status(metadata.get("legacy_status") or step.status)
            elif not first_pending_seen:
                result_status = "running"
                step_status = "skipped" if action == "reuse_checkpoint" else "running"
                first_pending_seen = True
            else:
                result_status = "queued"
                step_status = "skipped" if action == "reuse_checkpoint" else "queued"
            physical_skip = action == "reuse_checkpoint"
        elif worker_status == "paused":
            if previous_status in {"skipped_by_checkpoint", "executed_inline", "executed_by_background_worker"}:
                result_status = previous_status
                step_status = "skipped" if action == "reuse_checkpoint" else _step_status(metadata.get("legacy_status") or step.status)
            else:
                result_status = "paused"
                step_status = "skipped" if action == "reuse_checkpoint" else "paused"
            physical_skip = action == "reuse_checkpoint"
        else:
            result_status = "queued"
            step_status = "skipped" if action == "reuse_checkpoint" else "queued"
            physical_skip = action == "reuse_checkpoint"
        metadata.update(
            {
                "worker_action": action,
                "worker_status": result_status,
                "physical_skip": physical_skip,
            }
        )
        worker_results.append(
            {
                "step_key": step.step_key,
                "action": action,
                "worker_status": result_status,
                "status": step_status,
                "reason": plan.get("reason"),
                "artifact_reused": action == "reuse_checkpoint"
                and result_status == "skipped_by_checkpoint",
            }
        )
        updated_steps.append(step.model_copy(update={"status": step_status, "metadata": metadata}))
    metadata = dict(run.metadata)
    metadata.update(
        {
            "worker_mode": "async_plan_worker",
            "worker_status": worker_status,
            "worker_results": worker_results,
            "result_status_before_worker": metadata.get("result_status_before_worker")
            or (run.status if run.status not in {"queued", "running", "paused"} else "succeeded"),
        }
    )
    status = (
        worker_status
        if worker_status in {"queued", "running", "paused"}
        else metadata.get("result_status_before_worker", run.status)
    )
    return run.model_copy(
        update={
            "source": "analyze_contract.v3.async_worker",
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step_runs": updated_steps,
            "metadata": metadata,
        }
    )


def _apply_worker_step_result(run: WorkflowRunRecord, step_key: str, worker_status: str) -> WorkflowRunRecord:
    plan_by_step = {
        item.get("step_key"): item
        for item in (run.metadata.get("execution_plan") or [])
        if item.get("step_key")
    }
    existing_results = _worker_results_by_step(run)
    worker_results = []
    updated_steps = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for step in run.step_runs:
        plan = plan_by_step.get(step.step_key) or {}
        action = plan.get("action") or step.metadata.get("worker_action") or "execute"
        current_result = dict(existing_results.get(step.step_key) or {})
        result_status = current_result.get("worker_status") or "queued"
        if step.step_key == step_key:
            result_status = worker_status
        status = _step_status_for_worker_result(step, action, result_status)
        metadata = dict(step.metadata)
        metadata.update(
            {
                "worker_action": action,
                "worker_status": result_status,
                "physical_skip": action == "reuse_checkpoint",
            }
        )
        if result_status in {"skipped_by_checkpoint", "executed_inline", "executed_by_background_worker"}:
            metadata["artifact_reused"] = action == "reuse_checkpoint"
            metadata["artifact_key"] = f"{run.id}:{step.step_key}:{run.input_hash or 'no-input-hash'}"
            metadata["finished_by_worker_at"] = now
        worker_results.append(
            {
                "step_key": step.step_key,
                "action": action,
                "worker_status": result_status,
                "status": status,
                "reason": plan.get("reason"),
                "artifact_reused": action == "reuse_checkpoint"
                and result_status == "skipped_by_checkpoint",
            }
        )
        updated_steps.append(
            step.model_copy(update={"status": status, "updated_at": now, "metadata": metadata})
        )
    metadata = dict(run.metadata)
    metadata.update(
        {
            "worker_mode": "async_plan_worker",
            "worker_status": "running",
            "worker_results": worker_results,
        }
    )
    return run.model_copy(
        update={
            "source": "analyze_contract.v3.async_worker",
            "status": "running",
            "updated_at": now,
            "step_runs": updated_steps,
            "metadata": metadata,
        }
    )


def _apply_worker_failure(run: WorkflowRunRecord, error: str) -> WorkflowRunRecord:
    metadata = dict(run.metadata)
    metadata.update(
        {
            "worker_mode": "async_plan_worker",
            "worker_status": "failed",
            "worker_error": error,
        }
    )
    return run.model_copy(
        update={
            "source": "analyze_contract.v3.async_worker",
            "status": "failed",
            "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metadata": metadata,
        }
    )


def _worker_results_by_step(run: WorkflowRunRecord) -> dict[str, dict]:
    return {
        item.get("step_key"): item
        for item in (run.metadata.get("worker_results") or [])
        if isinstance(item, dict) and item.get("step_key")
    }


def _next_pending_worker_step(run: WorkflowRunRecord) -> dict | None:
    completed = {
        item.get("step_key")
        for item in (run.metadata.get("worker_results") or [])
        if item.get("worker_status") in {"skipped_by_checkpoint", "executed_inline", "executed_by_background_worker"}
    }
    for item in run.metadata.get("execution_plan") or []:
        step_key = item.get("step_key")
        if step_key and step_key not in completed:
            return item
    return None


def _step_status_for_worker_result(step: StepRunRecord, action: str, worker_status: str) -> str:
    if worker_status == "skipped_by_checkpoint":
        return "skipped"
    if worker_status in {"executed_inline", "executed_by_background_worker"}:
        return _step_status(step.metadata.get("legacy_status") or step.status)
    if worker_status in {"running", "paused", "queued"}:
        return worker_status
    return "failed" if worker_status == "failed" else step.status


def _checkpoint_for_step(task: TaskRecord, step_key: str) -> dict:
    return next(
        (payload for payload in _checkpoint_payloads(task) if payload.get("step_key") == step_key),
        {},
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
