from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.services.assets import AssetNotFoundError, AssetRegistry, AssetStateError, asset_counts
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


class CreateAssetRequest(BaseModel):
    asset_type: str
    name: str
    applicability: dict = Field(default_factory=dict)
    content: dict = Field(default_factory=dict)
    schema_version: str | None = None
    description: str | None = None
    actor: str = "reviewer"


class AssetReviewRequest(BaseModel):
    actor: str = "reviewer"
    comment: str | None = None


class CloneAssetRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    actor: str = "reviewer"


class CreateSourceDocumentRequest(BaseModel):
    name: str
    source_text: str
    source_type: str = "policy_document"
    metadata: dict = Field(default_factory=dict)
    actor: str = "reviewer"


class RuleDraftGenerateRequest(BaseModel):
    source_type: str = "policy_document"
    source_text: str | None = None
    source_document_id: str | None = None
    profile_hint: dict = Field(default_factory=dict)
    draft_types: list[str] = Field(default_factory=lambda: ["policy_reference", "hard_rule", "semantic_rule", "extraction_rule"])
    actor: str = "reviewer"


class CloneProfileRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    actor: str = "reviewer"


class BindProfileAssetRequest(BaseModel):
    asset_id: str
    required: bool = True
    binding_reason: str | None = None


class PublishProfileRequest(BaseModel):
    actor: str = "reviewer"
    comment: str | None = None


@router.get("/tasks")
def list_tasks() -> dict:
    try:
        tasks = [build_task_summary(task) for task in TaskRepository().list_tasks()]
    except TaskStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"items": tasks, "total": len(tasks)}


@router.get("/review-profiles")
def list_review_profiles(status: str | None = "active", contract_type: str | None = None) -> dict:
    profiles = AssetRegistry().list_profiles(status=status, contract_type=contract_type)
    return {
        "items": [
            {
                **profile.model_dump(exclude={"assets"}),
                "asset_counts": asset_counts(profile),
            }
            for profile in profiles
        ],
        "total": len(profiles),
    }


@router.get("/assets")
def list_assets(asset_type: str | None = None, status: str | None = None, q: str | None = None) -> dict:
    registry = AssetRegistry()
    assets = registry.list_assets(asset_type=asset_type, status=status, q=q)
    return {
        "items": [
            {
                **asset.model_dump(),
                "execution_status": registry.execution_status_for_asset_type(asset.asset_type),
            }
            for asset in assets
        ],
        "total": len(assets),
    }


@router.get("/assets/execution-audit")
def asset_execution_audit() -> dict:
    return AssetRegistry().asset_execution_audit()


@router.get("/asset-source-documents")
def list_asset_source_documents(q: str | None = None) -> dict:
    documents = AssetRegistry().list_source_documents(q=q)
    return {
        "items": [
            {
                **document.model_dump(exclude={"content_text"}),
                "content_preview": document.content_text[:240],
            }
            for document in documents
        ],
        "total": len(documents),
    }


@router.post("/asset-source-documents", status_code=201)
def create_asset_source_document(payload: CreateSourceDocumentRequest) -> JSONResponse:
    try:
        document = AssetRegistry().create_source_document(
            name=payload.name,
            source_text=payload.source_text,
            source_type=payload.source_type,
            metadata=payload.metadata,
            actor=payload.actor,
        )
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"document": document.model_dump()})


@router.get("/asset-source-documents/{document_id}")
def get_asset_source_document(document_id: str) -> dict:
    try:
        document = AssetRegistry().get_source_document(document_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"document": document.model_dump()}


@router.delete("/asset-source-documents/{document_id}", status_code=204)
def delete_asset_source_document(document_id: str) -> None:
    try:
        AssetRegistry().delete_source_document(document_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/llm-executions")
def list_llm_executions(purpose: str | None = None) -> dict:
    executions = AssetRegistry().list_llm_executions(purpose=purpose)
    return {"items": [execution.model_dump() for execution in executions], "total": len(executions)}


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str) -> dict:
    registry = AssetRegistry()
    try:
        asset = registry.get_asset(asset_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "asset": {
            **asset.model_dump(),
            "execution_status": registry.execution_status_for_asset_type(asset.asset_type),
        }
    }


@router.post("/assets", status_code=201)
def create_asset(payload: CreateAssetRequest) -> JSONResponse:
    try:
        asset = AssetRegistry().create_asset_draft(
            asset_type=payload.asset_type,
            name=payload.name,
            applicability=payload.applicability,
            content=payload.content,
            schema_version=payload.schema_version,
            description=payload.description,
            actor=payload.actor,
        )
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"asset": asset.model_dump()})


@router.post("/assets/{asset_id}/versions", status_code=201)
def clone_asset(asset_id: str, payload: CloneAssetRequest) -> JSONResponse:
    try:
        asset = AssetRegistry().clone_asset(
            asset_id,
            name=payload.name,
            description=payload.description,
            actor=payload.actor,
        )
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"asset": asset.model_dump()})


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: str) -> None:
    try:
        AssetRegistry().delete_asset(asset_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/assets/{asset_id}/approve")
def approve_asset(asset_id: str, payload: AssetReviewRequest) -> dict:
    try:
        asset = AssetRegistry().approve_asset(asset_id, actor=payload.actor, comment=payload.comment)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"asset": asset.model_dump()}


@router.post("/assets/{asset_id}/reject")
def reject_asset(asset_id: str, payload: AssetReviewRequest) -> dict:
    try:
        asset = AssetRegistry().reject_asset(asset_id, actor=payload.actor, comment=payload.comment)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"asset": asset.model_dump()}


@router.post("/assets/{asset_id}/publish")
def publish_asset(asset_id: str, payload: AssetReviewRequest) -> dict:
    try:
        asset = AssetRegistry().publish_asset(asset_id, actor=payload.actor)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"asset": asset.model_dump()}


@router.post("/rule-drafts/generate", status_code=201)
def generate_rule_drafts(payload: RuleDraftGenerateRequest) -> JSONResponse:
    try:
        result = AssetRegistry().generate_rule_drafts(
            source_text=payload.source_text,
            source_document_id=payload.source_document_id,
            source_type=payload.source_type,
            draft_types=payload.draft_types,
            profile_hint=payload.profile_hint,
            actor=payload.actor,
        )
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        status_code=201,
        content={
            "drafts": [asset.model_dump() for asset in result["drafts"]],
            "llm_execution": result["llm_execution"],
        },
    )


@router.get("/review-profiles/{profile_id}")
def get_review_profile(profile_id: str) -> dict:
    registry = AssetRegistry()
    try:
        profile = registry.get_profile(profile_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    assets = []
    for ref in profile.assets:
        try:
            asset = registry.get_asset(ref.asset_id)
        except AssetNotFoundError:
            continue
        assets.append({"ref": ref.model_dump(), "asset": asset.model_dump()})
    return {
        "profile": profile.model_dump(),
        "assets": assets,
        "asset_counts": asset_counts(profile),
        "execution_audit": registry.profile_execution_audit(profile),
    }


@router.post("/review-profiles/{profile_id}/versions", status_code=201)
def clone_review_profile(profile_id: str, payload: CloneProfileRequest) -> JSONResponse:
    try:
        profile = AssetRegistry().clone_profile(
            profile_id,
            name=payload.name,
            description=payload.description,
            actor=payload.actor,
        )
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content={"profile": profile.model_dump()})


@router.post("/review-profiles/{profile_id}/assets")
def bind_profile_asset(profile_id: str, payload: BindProfileAssetRequest) -> dict:
    try:
        profile = AssetRegistry().bind_asset_to_profile(
            profile_id,
            payload.asset_id,
            required=payload.required,
            binding_reason=payload.binding_reason,
        )
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile.model_dump(), "asset_counts": asset_counts(profile)}


@router.post("/review-profiles/{profile_id}/publish")
def publish_review_profile(profile_id: str, payload: PublishProfileRequest) -> dict:
    try:
        profile = AssetRegistry().publish_profile(profile_id, actor=payload.actor, comment=payload.comment)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile.model_dump(), "asset_counts": asset_counts(profile)}


@router.post("/tasks", status_code=201)
async def create_task(
    contract_name: str | None = Form(default=None),
    selected_profile_id: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> JSONResponse:
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

    payload = build_task_summary(task)
    payload["overall_risk"] = task.overall_risk
    payload["selected_profile_id"] = task.selected_profile_id
    payload["selected_profile_name"] = task.selected_profile_name
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
