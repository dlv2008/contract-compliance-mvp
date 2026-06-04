from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.assets import AssetNotFoundError, AssetRegistry, AssetStateError, asset_counts
from app.services.db_store import DatabaseProbeClient
from app.services.llm import LLMClient
from app.services.object_store import ObjectStore
from app.services.ragflow import RagflowClient
from app.services.review_engine import build_dashboard_payload, build_review_payload
from app.services.storage import ContractUploadError, TaskRepository, TaskStorageError

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()


def _dashboard_data(tasks: list) -> dict:  # noqa: ANN001
    data = build_dashboard_payload(
        tasks,
        RagflowClient().probe(),
        LLMClient().probe(),
        DatabaseProbeClient().probe(),
        ObjectStore().probe(),
    )
    registry = AssetRegistry()
    data["review_profiles"] = [profile.model_dump() for profile in registry.list_profiles(status="active")]
    data["asset_summary"] = registry.summary()
    return data


def _redirect_with_asset_error(target: str, exc: Exception) -> RedirectResponse:
    return RedirectResponse(url=f"{target}?error={str(exc)}", status_code=303)


def _asset_workbench_data(
    *,
    asset_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    error: str | None = None,
) -> dict:
    registry = AssetRegistry()
    assets = registry.list_assets(asset_type=asset_type or None, status=status or None, q=q or None)
    profiles = registry.list_profiles(status=None)
    return {
        "assets": [
            {
                **asset.model_dump(),
                "execution_status": registry.execution_status_for_asset_type(asset.asset_type),
            }
            for asset in assets
        ],
        "profiles": [profile.model_dump() for profile in profiles],
        "summary": registry.summary(),
        "execution_audit": registry.asset_execution_audit(),
        "asset_types": registry.asset_types(),
        "selected_asset_type": asset_type or "",
        "selected_status": status or "",
        "query": q or "",
        "error": error,
    }


def _profile_detail_data(profile_id: str, *, error: str | None = None) -> dict:
    registry = AssetRegistry()
    profile = registry.get_profile(profile_id)
    refs = []
    for ref in profile.assets:
        try:
            asset = registry.get_asset(ref.asset_id)
        except AssetNotFoundError:
            continue
        refs.append(
            {
                "ref": ref.model_dump(),
                "asset": asset.model_dump(),
                "execution_status": registry.execution_status_for_asset_type(asset.asset_type),
            }
        )
    return {
        "profile": profile.model_dump(),
        "asset_refs": refs,
        "asset_counts": asset_counts(profile),
        "execution_audit": registry.profile_execution_audit(profile),
        "active_assets": [asset.model_dump() for asset in registry.list_assets(status="active")],
        "error": error,
    }


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
            "data": _dashboard_data(tasks),
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
            "data": _dashboard_data(task_items),
        },
    )


@router.get("/assets", response_class=HTMLResponse)
def assets_workbench(
    request: Request,
    asset_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "assets.html",
        {
            "page_title": "审查配置资产",
            "data": _asset_workbench_data(asset_type=asset_type, status=status, q=q, error=error),
        },
    )


@router.post("/assets/create")
def create_asset_draft(
    asset_type: str = Form(...),
    name: str = Form(...),
    description: str | None = Form(default=None),
    contract_type: str | None = Form(default=None),
    fact_key: str | None = Form(default=None),
    operator: str | None = Form(default=None),
    value: str | None = Form(default=None),
) -> RedirectResponse:
    applicability = {"contract_type": contract_type} if contract_type else {}
    content = {}
    if fact_key:
        content["fact_key"] = fact_key
    if operator:
        content["operator"] = operator
    if value:
        try:
            content["value"] = float(value)
        except ValueError:
            content["value"] = value
    if asset_type == "hard_rule":
        content.setdefault("risk_level", "high")
    try:
        AssetRegistry().create_asset_draft(
            asset_type=asset_type,
            name=name,
            applicability=applicability,
            content=content,
            description=description,
        )
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/assets", exc)
    return RedirectResponse(url="/assets?status=draft", status_code=303)


@router.post("/assets/{asset_id}/versions")
def clone_asset_form(
    asset_id: str,
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        draft = AssetRegistry().clone_asset(asset_id, name=name, description=description)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/assets", exc)
    return RedirectResponse(url=f"/assets?status=draft&q={draft.id}", status_code=303)


@router.post("/assets/{asset_id}/approve")
def approve_asset_form(asset_id: str, comment: str | None = Form(default=None)) -> RedirectResponse:
    try:
        AssetRegistry().approve_asset(asset_id, comment=comment)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/assets", exc)
    return RedirectResponse(url="/assets", status_code=303)


@router.post("/assets/{asset_id}/reject")
def reject_asset_form(asset_id: str, comment: str | None = Form(default=None)) -> RedirectResponse:
    try:
        AssetRegistry().reject_asset(asset_id, comment=comment)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/assets", exc)
    return RedirectResponse(url="/assets", status_code=303)


@router.post("/assets/{asset_id}/publish")
def publish_asset_form(asset_id: str) -> RedirectResponse:
    try:
        AssetRegistry().publish_asset(asset_id)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/assets", exc)
    return RedirectResponse(url="/assets?status=active", status_code=303)


@router.get("/rule-drafts", response_class=HTMLResponse)
def rule_drafts(request: Request, error: str | None = None) -> HTMLResponse:
    registry = AssetRegistry()
    drafts = [asset.model_dump() for asset in registry.list_assets(status="draft")]
    approved = [asset.model_dump() for asset in registry.list_assets(status="approved")]
    return templates.TemplateResponse(
        request,
        "rule_drafts.html",
        {
            "page_title": "规则草稿审核",
            "data": {
                "drafts": drafts,
                "approved": approved,
                "profiles": [profile.model_dump() for profile in registry.list_profiles(status=None)],
                "error": error,
            },
        },
    )


@router.post("/rule-drafts/generate")
def generate_rule_drafts_form(
    source_text: str = Form(...),
    contract_type: str = Form(default="procurement_contract"),
    include_semantic_rule: bool = Form(default=False),
    include_message_template: bool = Form(default=False),
) -> RedirectResponse:
    draft_types = ["hard_rule"]
    if include_semantic_rule:
        draft_types.append("semantic_rule")
    if include_message_template:
        draft_types.append("risk_message_template")
    try:
        AssetRegistry().generate_rule_drafts(
            source_text=source_text,
            profile_hint={"contract_type": contract_type},
            draft_types=draft_types,
        )
    except AssetStateError as exc:
        return _redirect_with_asset_error("/rule-drafts", exc)
    return RedirectResponse(url="/rule-drafts", status_code=303)


@router.get("/review-profiles/{profile_id}", response_class=HTMLResponse)
def review_profile_detail(request: Request, profile_id: str, error: str | None = None) -> HTMLResponse:
    try:
        data = _profile_detail_data(profile_id, error=error)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "page_title": "审查配置集",
            "data": data,
        },
    )


@router.post("/review-profiles/{profile_id}/versions")
def clone_profile_form(
    profile_id: str,
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        profile = AssetRegistry().clone_profile(profile_id, name=name, description=description)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error(f"/review-profiles/{profile_id}", exc)
    return RedirectResponse(url=f"/review-profiles/{profile.id}", status_code=303)


@router.post("/review-profiles/{profile_id}/assets")
def bind_profile_asset_form(
    profile_id: str,
    asset_id: str = Form(...),
    binding_reason: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        AssetRegistry().bind_asset_to_profile(profile_id, asset_id, binding_reason=binding_reason)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error(f"/review-profiles/{profile_id}", exc)
    return RedirectResponse(url=f"/review-profiles/{profile_id}", status_code=303)


@router.post("/review-profiles/{profile_id}/publish")
def publish_profile_form(profile_id: str, comment: str | None = Form(default=None)) -> RedirectResponse:
    try:
        AssetRegistry().publish_profile(profile_id, comment=comment)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error(f"/review-profiles/{profile_id}", exc)
    return RedirectResponse(url=f"/review-profiles/{profile_id}", status_code=303)


@router.post("/tasks/create")
async def create_task(
    contract_name: str | None = Form(default=None),
    selected_profile_id: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> RedirectResponse:
    try:
        task = TaskRepository().create_task_from_upload(
            filename=file.filename or "contract.txt",
            payload=await file.read(),
            contract_name=contract_name,
            content_type=file.content_type,
            selected_profile_id=selected_profile_id,
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
                TaskRepository().list_report_snapshots(task_id),
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


@router.post("/reviews/{task_id}/task-decision")
def create_task_decision(
    task_id: str,
    action_type: str = Form(...),
    comment: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        TaskRepository().record_task_decision(
            task_id,
            action_type=action_type,
            comment=comment,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url=f"/reviews/{task_id}", status_code=303)


@router.post("/reviews/{task_id}/reports")
def generate_report(
    task_id: str,
    comment: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        TaskRepository().generate_delivery_report(
            task_id,
            comment=comment,
        )
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url=f"/reviews/{task_id}", status_code=303)
