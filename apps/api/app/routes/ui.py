from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.db_store import DatabaseProbeClient
from app.services.llm import LLMClient
from app.services.object_store import ObjectStore
from app.services.ragflow import RagflowClient
from app.services.review_engine import build_dashboard_payload, build_review_payload
from app.services.storage import ContractUploadError, TaskRepository, TaskStorageError

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    try:
        tasks = TaskRepository().list_tasks()
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "page_title": "合同合规审查工作台",
            "data": build_dashboard_payload(
                tasks,
                RagflowClient().probe(),
                LLMClient().probe(),
                DatabaseProbeClient().probe(),
                ObjectStore().probe(),
            ),
        },
    )


@router.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request) -> HTMLResponse:
    try:
        task_items = TaskRepository().list_tasks()
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "page_title": "任务总览",
            "data": build_dashboard_payload(
                task_items,
                RagflowClient().probe(),
                LLMClient().probe(),
                DatabaseProbeClient().probe(),
                ObjectStore().probe(),
            ),
        },
    )


@router.post("/tasks/create")
async def create_task(
    contract_name: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> RedirectResponse:
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
    return RedirectResponse(url=f"/reviews/{task.id}", status_code=303)


@router.get("/reviews/latest")
def latest_review() -> RedirectResponse:
    try:
        tasks = TaskRepository().list_tasks()
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not tasks:
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url=f"/reviews/{tasks[0].id}", status_code=303)


@router.get("/reviews/{task_id}", response_class=HTMLResponse)
def review(task_id: str, request: Request) -> HTMLResponse:
    try:
        task = TaskRepository().get_task(task_id)
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "page_title": "审查结果",
            "data": build_review_payload(
                task,
                RagflowClient().probe(),
                LLMClient().probe(),
                DatabaseProbeClient().probe(),
                ObjectStore().probe(),
            ),
        },
    )


@router.post("/reviews/{task_id}/actions")
def create_review_action(
    task_id: str,
    target_id: str = Form(...),
    action_type: str = Form(...),
    comment: str | None = Form(default=None),
    revised_action: str | None = Form(default=None),
) -> RedirectResponse:
    revised_payload = {}
    if action_type == "rewrite_suggestion" and revised_action:
        revised_payload["action"] = revised_action
    try:
        TaskRepository().record_review_action(
            task_id,
            target_type="rule_hit",
            target_id=target_id,
            action_type=action_type,
            comment=comment,
            revised_payload=revised_payload,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url=f"/reviews/{task_id}", status_code=303)
