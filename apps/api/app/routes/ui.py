import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.assets import AssetNotFoundError, AssetRegistry, AssetStateError, asset_counts
from app.services.db_store import DatabaseProbeClient
from app.services.llm import LLMClient
from app.services.object_store import ObjectStore
from app.services.profile_dry_run import ProfileDryRunError, ProfileDryRunService
from app.services.ragflow import RagflowClient
from app.services.review_engine import build_dashboard_payload, build_review_payload
from app.services.storage import ContractUploadError, TaskRepository, TaskStorageError
from app.services.workflow_runs import WorkflowRunRepository

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
    audit_events = registry.list_audit_events(limit=12)
    edit_locks = registry.list_edit_locks()
    return {
        "assets": [
            {
                **asset.model_dump(),
                "execution_status": registry.execution_status_for_asset_type(asset.asset_type),
            }
            for asset in assets
        ],
        "profiles": [profile.model_dump() for profile in profiles],
        "audit_events": [event.model_dump() for event in audit_events],
        "edit_locks": [lock.model_dump() for lock in edit_locks],
        "summary": registry.summary(),
        "storage": {
            "backend": registry.settings.asset_store_backend,
            "database_configured": bool(registry.settings.database_url),
        },
        "execution_audit": registry.asset_execution_audit(),
        "asset_types": registry.asset_types(),
        "selected_asset_type": asset_type or "",
        "selected_status": status or "",
        "query": q or "",
        "error": error,
    }


def _asset_audit_data(
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    limit: int = 100,
) -> dict:
    registry = AssetRegistry()
    resolved_limit = max(1, min(limit, 200))
    events = registry.list_audit_events(
        target_type=target_type or None,
        target_id=target_id or None,
        action=action or None,
        limit=500,
    )
    if actor:
        events = [event for event in events if event.actor == actor]
    events = events[:resolved_limit]
    return {
        "events": [event.model_dump() for event in events],
        "total": len(events),
        "filters": {
            "target_type": target_type or "",
            "target_id": target_id or "",
            "action": action or "",
            "actor": actor or "",
            "limit": resolved_limit,
        },
        "summary": registry.summary(),
        "edit_locks": [lock.model_dump() for lock in registry.list_edit_locks()],
    }


def _profile_detail_data(
    profile_id: str,
    *,
    error: str | None = None,
    dry_run_id: str | None = None,
) -> dict:
    registry = AssetRegistry()
    profile = registry.get_profile(profile_id)
    dry_run_service = ProfileDryRunService(registry=registry)
    dry_runs = dry_run_service.list_records(profile_id, limit=5)
    selected_dry_run = None
    if dry_run_id:
        try:
            selected_dry_run = dry_run_service.get_record(dry_run_id)
        except ProfileDryRunError:
            selected_dry_run = None
    if selected_dry_run is None and dry_runs:
        selected_dry_run = dry_runs[0]
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
        "dry_runs": [record.model_dump() for record in dry_runs],
        "latest_dry_run": dry_runs[0].model_dump() if dry_runs else None,
        "selected_dry_run": selected_dry_run.model_dump() if selected_dry_run else None,
        "dry_run_gate": dry_run_service.publication_gate_status(profile),
        "error": error,
    }


def _draft_asset_view(asset, registry: AssetRegistry) -> dict:  # noqa: ANN001
    validation_error = None
    try:
        registry.validate_asset_draft(asset)
    except AssetStateError as exc:
        validation_error = str(exc)
    return {
        **asset.model_dump(),
        "applicability_json": json.dumps(asset.applicability, ensure_ascii=False, indent=2),
        "content_json": json.dumps(asset.content, ensure_ascii=False, indent=2),
        "validation_error": validation_error,
        "source_document_id": asset.content.get("source_document_id"),
        "source_chunk_ids": asset.content.get("source_chunk_ids") or [],
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


@router.get("/asset-audit", response_class=HTMLResponse)
def asset_audit(
    request: Request,
    target_type: str | None = None,
    target_id: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    limit: int = 100,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "asset_audit.html",
        {
            "page_title": "资产操作审计",
            "data": _asset_audit_data(
                target_type=target_type,
                target_id=target_id,
                action=action,
                actor=actor,
                limit=limit,
            ),
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
def rule_drafts(
    request: Request,
    error: str | None = None,
    source_document_id: str | None = None,
    latest_execution_id: str | None = None,
) -> HTMLResponse:
    registry = AssetRegistry()
    drafts = [_draft_asset_view(asset, registry) for asset in registry.list_assets(status="draft")]
    approved = [asset.model_dump() for asset in registry.list_assets(status="approved")]
    source_documents = registry.list_source_documents()
    selected_source_document = None
    if source_document_id:
        try:
            selected_source_document = registry.get_source_document(source_document_id).model_dump()
        except AssetNotFoundError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "rule_drafts.html",
        {
            "page_title": "规则草稿审核",
            "data": {
                "drafts": drafts,
                "approved": approved,
                "source_documents": [document.model_dump(exclude={"content_text"}) for document in source_documents],
                "selected_source_document": selected_source_document,
                "llm_executions": [
                    execution.model_dump() for execution in registry.list_llm_executions(purpose="rule_draft")[:5]
                ],
                "latest_execution_id": latest_execution_id,
                "profiles": [profile.model_dump() for profile in registry.list_profiles(status=None)],
                "error": error,
            },
        },
    )


@router.post("/asset-source-documents/create")
async def create_asset_source_document_form(
    name: str = Form(...),
    source_type: str = Form(default="policy_document"),
    source_text: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> RedirectResponse:
    uploaded_text = ""
    filename = None
    if file and file.filename:
        filename = file.filename
        uploaded_text = (await file.read()).decode("utf-8", errors="ignore")
    resolved_text = (uploaded_text or source_text or "").strip()
    resolved_name = name.strip() or filename or "未命名制度文档"
    try:
        document = AssetRegistry().create_source_document(
            name=resolved_name,
            source_text=resolved_text,
            source_type=source_type,
            metadata={"filename": filename} if filename else {},
        )
    except AssetStateError as exc:
        return _redirect_with_asset_error("/rule-drafts", exc)
    return RedirectResponse(url=f"/rule-drafts?source_document_id={document.id}", status_code=303)


@router.post("/asset-source-documents/{document_id}/delete")
def delete_asset_source_document_form(document_id: str) -> RedirectResponse:
    try:
        AssetRegistry().delete_source_document(document_id)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/rule-drafts", exc)
    return RedirectResponse(url="/rule-drafts", status_code=303)


@router.post("/rule-drafts/generate")
def generate_rule_drafts_form(
    source_text: str | None = Form(default=None),
    source_document_id: str | None = Form(default=None),
    contract_type: str = Form(default="procurement_contract"),
    include_policy_reference: bool = Form(default=False),
    include_semantic_rule: bool = Form(default=False),
    include_extraction_rule: bool = Form(default=False),
) -> RedirectResponse:
    draft_types = ["hard_rule"]
    if include_policy_reference:
        draft_types.insert(0, "policy_reference")
    if include_semantic_rule:
        draft_types.append("semantic_rule")
    if include_extraction_rule:
        draft_types.append("extraction_rule")
    try:
        result = AssetRegistry().generate_rule_drafts(
            source_text=source_text,
            source_document_id=source_document_id or None,
            profile_hint={"contract_type": contract_type},
            draft_types=draft_types,
        )
    except AssetStateError as exc:
        return _redirect_with_asset_error("/rule-drafts", exc)
    params = []
    if source_document_id:
        params.append(f"source_document_id={source_document_id}")
    params.append(f"latest_execution_id={result['llm_execution']['id']}")
    return RedirectResponse(url=f"/rule-drafts?{'&'.join(params)}", status_code=303)


@router.post("/assets/{asset_id}/delete")
def delete_asset_form(asset_id: str) -> RedirectResponse:
    try:
        AssetRegistry().delete_asset(asset_id)
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/rule-drafts", exc)
    return RedirectResponse(url="/rule-drafts", status_code=303)


@router.post("/assets/{asset_id}/update-draft")
def update_asset_draft_form(
    asset_id: str,
    name: str = Form(...),
    schema_version: str = Form(...),
    description: str | None = Form(default=None),
    applicability_json: str = Form(...),
    content_json: str = Form(...),
    expected_content_hash: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        applicability = json.loads(applicability_json)
        content = json.loads(content_json)
    except json.JSONDecodeError as exc:
        return _redirect_with_asset_error("/rule-drafts", AssetStateError(f"JSON 格式错误：{exc.msg}"))
    if not isinstance(applicability, dict):
        return _redirect_with_asset_error("/rule-drafts", AssetStateError("适用性必须是 JSON 对象。"))
    if not isinstance(content, dict):
        return _redirect_with_asset_error("/rule-drafts", AssetStateError("内容必须是 JSON 对象。"))
    try:
        AssetRegistry().update_asset_draft(
            asset_id,
            name=name,
            description=description,
            applicability=applicability,
            content=content,
            schema_version=schema_version,
            expected_content_hash=expected_content_hash,
        )
    except (AssetNotFoundError, AssetStateError) as exc:
        return _redirect_with_asset_error("/rule-drafts", exc)
    return RedirectResponse(url=f"/rule-drafts?latest_execution_id={content.get('llm_execution_id', '')}", status_code=303)


@router.get("/review-profiles/{profile_id}", response_class=HTMLResponse)
def review_profile_detail(
    request: Request,
    profile_id: str,
    error: str | None = None,
    dry_run_id: str | None = None,
) -> HTMLResponse:
    try:
        data = _profile_detail_data(profile_id, error=error, dry_run_id=dry_run_id)
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


@router.post("/review-profiles/{profile_id}/dry-run")
def dry_run_profile_form(
    profile_id: str,
    contract_name: str | None = Form(default=None),
    source_text: str = Form(...),
) -> RedirectResponse:
    try:
        record = ProfileDryRunService().run(
            profile_id,
            contract_name=contract_name,
            source_filename="profile-dry-run.txt",
            source_text=source_text,
        )
    except ProfileDryRunError as exc:
        return _redirect_with_asset_error(f"/review-profiles/{profile_id}", exc)
    return RedirectResponse(url=f"/review-profiles/{profile_id}?dry_run_id={record.id}", status_code=303)


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
    registry = AssetRegistry()
    dry_run_service = ProfileDryRunService(registry=registry)
    try:
        draft_profile = registry.get_profile(profile_id)
        if draft_profile.status == "draft":
            dry_run_service.assert_profile_can_publish(draft_profile)
        registry.publish_profile(profile_id, comment=comment)
    except (AssetNotFoundError, AssetStateError, ProfileDryRunError) as exc:
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
                WorkflowRunRepository().latest_for_task(task_id),
            ),
        },
    )


@router.post("/reviews/{task_id}/workflow-run/steps/{step_key}/retry")
def retry_review_workflow_step(task_id: str, step_key: str) -> RedirectResponse:
    try:
        TaskRepository().retry_workflow_step(task_id, step_key)
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url=f"/reviews/{task_id}", status_code=303)


@router.post("/reviews/{task_id}/workflow-run/resume")
def resume_review_workflow_run(
    task_id: str,
    resume_from_step: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        TaskRepository().resume_workflow_run(task_id, resume_from_step=resume_from_step or None)
    except ContractUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url=f"/reviews/{task_id}", status_code=303)


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
