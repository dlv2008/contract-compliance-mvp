from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.services.db_store import DatabaseProbeClient
from app.services.llm import LLMClient
from app.services.object_store import ObjectStore
from app.services.ragflow import RagflowClient
from app.services.review_engine import build_review_payload, build_task_summary
from app.services.storage import ContractUploadError, TaskRepository, TaskStorageError


router = APIRouter()


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
    try:
        task = TaskRepository().get_task(task_id)
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
        )
    }


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
