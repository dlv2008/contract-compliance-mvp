from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.services.db_store import DatabaseProbeClient
from app.services.llm import LLMClient
from app.services.object_store import ObjectStore
from app.services.ragflow import RagflowClient
from app.services.review_engine import build_review_payload, build_task_summary
from app.services.storage import ContractUploadError, TaskRepository, TaskStorageError


router = APIRouter()


class ReviewActionRequest(BaseModel):
    target_type: str = Field(default="rule_hit")
    target_id: str
    action_type: str
    actor: str = "reviewer"
    comment: str | None = None
    revised_payload: dict = Field(default_factory=dict)


class TaskDecisionRequest(BaseModel):
    action_type: str
    actor: str = "reviewer"
    comment: str | None = None


class GenerateReportRequest(BaseModel):
    actor: str = "reviewer"
    comment: str | None = None


@router.get("/tasks")
def list_tasks() -> dict:
    try:
        tasks = [build_task_summary(task) for task in TaskRepository().list_tasks()]
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"items": tasks, "total": len(tasks)}


@router.post("/tasks", status_code=201)
async def create_task(
    contract_name: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> JSONResponse:
    try:
        task = TaskRepository().create_task_from_upload(
            filename=file.filename or "contract.txt",
            payload=await file.read(),
            contract_name=contract_name,
            content_type=file.content_type,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = build_task_summary(task)
    payload["overall_risk"] = task.overall_risk
    payload["review_url"] = f"/reviews/{task.id}"
    return JSONResponse(status_code=201, content={"task": payload})


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    repository = TaskRepository()
    try:
        task = repository.get_task(task_id)
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return {
        "task": build_review_payload(
            task,
            RagflowClient().probe(),
            LLMClient().probe(),
            DatabaseProbeClient().probe(),
            ObjectStore().probe(),
            repository.list_report_snapshots(task_id),
        )
    }


@router.get("/tasks/{task_id}/clauses")
def list_task_clauses(task_id: str) -> dict:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    return {"items": repository.list_document_clauses(task_id)}


@router.get("/tasks/{task_id}/facts")
def list_task_facts(task_id: str) -> dict:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    return {"items": repository.list_extracted_facts(task_id)}


@router.get("/tasks/{task_id}/rule-hits")
def list_task_rule_hits(task_id: str) -> dict:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    return {"items": repository.list_rule_hits(task_id)}


@router.get("/tasks/{task_id}/review-actions")
def list_task_review_actions(task_id: str) -> dict:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    return {"items": repository.list_review_actions(task_id)}


@router.post("/tasks/{task_id}/review-actions", status_code=201)
def create_task_review_action(task_id: str, payload: ReviewActionRequest) -> JSONResponse:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    try:
        action = repository.record_review_action(
            task_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            action_type=payload.action_type,
            actor=payload.actor,
            comment=payload.comment,
            revised_payload=payload.revised_payload,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"action": action.model_dump()})


@router.post("/tasks/{task_id}/task-decisions", status_code=201)
def create_task_decision(task_id: str, payload: TaskDecisionRequest) -> JSONResponse:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    try:
        action = repository.record_task_decision(
            task_id,
            action_type=payload.action_type,
            actor=payload.actor,
            comment=payload.comment,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"action": action.model_dump()})


@router.get("/tasks/{task_id}/report-snapshots")
def list_task_report_snapshots(task_id: str) -> dict:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    return {"items": repository.list_report_snapshots(task_id)}


@router.post("/tasks/{task_id}/reports", status_code=201)
def generate_task_report(task_id: str, payload: GenerateReportRequest) -> JSONResponse:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    try:
        report = repository.generate_delivery_report(
            task_id,
            actor=payload.actor,
            comment=payload.comment,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"report": report.model_dump()})


@router.get("/tasks/{task_id}/reports/{version}")
def get_task_report_markdown(task_id: str, version: int) -> PlainTextResponse:
    repository = TaskRepository()
    _ensure_task_exists(repository, task_id)
    try:
        content = repository.read_report_markdown(task_id, version)
    except ContractUploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")


@router.get("/ragflow/health")
def ragflow_health() -> dict:
    return RagflowClient().probe().model_dump()


@router.get("/llm/health")
def llm_health() -> dict:
    return LLMClient().probe().model_dump()


@router.post("/llm/check")
def llm_check() -> dict:
    return LLMClient().manual_check().model_dump()


@router.get("/system/status")
def system_status() -> dict:
    repository = TaskRepository()
    try:
        tasks = repository.list_tasks()
        storage = {"status": "ok", "task_count": len(tasks)}
    except TaskStorageError as exc:
        storage = {"status": "error", "detail": str(exc), "task_count": 0}
    return {
        "app": {"status": "ok"},
        "storage": storage,
        "database": DatabaseProbeClient().probe().model_dump(),
        "object_storage": ObjectStore().probe().model_dump(),
        "ragflow": RagflowClient().probe().model_dump(),
        "llm": LLMClient().probe().model_dump(),
    }


def _ensure_task_exists(repository: TaskRepository, task_id: str) -> None:
    try:
        task = repository.get_task(task_id)
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task does not exist.")
