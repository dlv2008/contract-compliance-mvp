from pathlib import Path
import sys
import time

import pytest
from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CONTRACT_COMPLIANCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTRACT_COMPLIANCE_BOOTSTRAP_SAMPLES", "0")
    monkeypatch.setenv("CONTRACT_COMPLIANCE_TASK_STORE_BACKEND", "json")
    monkeypatch.setenv("CONTRACT_COMPLIANCE_OBJECT_STORAGE", "local")
    monkeypatch.setenv("RAGFLOW_BASE_URL", "http://127.0.0.1:65530")
    monkeypatch.setenv("LLM_API_KEY", "test-secret-value")
    monkeypatch.setenv("LLM_PROBE_ENABLED", "0")
    monkeypatch.setenv("CONTRACT_COMPLIANCE_LLM_DRAFT_PROVIDER", "mock")

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def sample_contract(filename: str) -> Path:
    sample_dir = next(path for path in (REPO_ROOT / "resource").iterdir() if path.name.startswith("01_"))
    return sample_dir / filename


def dry_run_profile_for_publish(
    client: TestClient,
    profile_id: str,
    contract_text: str | None = None,
) -> dict:
    if contract_text is None:
        sample_dir = next(path for path in (REPO_ROOT / "resource").iterdir() if path.name.startswith("01_"))
        sample_path = sorted(path for path in sample_dir.iterdir() if "B-" in path.name)[-1]
        contract_text = sample_path.read_text(encoding="utf-8")
    response = client.post(
        f"/api/review-profiles/{profile_id}/dry-run",
        json={
            "contract_name": "publish gate dry-run",
            "source_filename": "publish-gate.txt",
            "source_text": contract_text,
        },
    )
    assert response.status_code == 201
    return response.json()["dry_run"]


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_endpoint(client: TestClient) -> None:
    response = client.get("/api/docs")

    assert response.status_code == 200


def test_asset_registry_uses_injected_json_store(tmp_path: Path) -> None:
    from app.services.assets import AssetRegistry, JsonAssetStore

    store_path = tmp_path / "asset-store" / "assets.json"
    registry = AssetRegistry(store=JsonAssetStore(store_path))

    assert store_path.exists() is False
    assert any(asset.id == "asset-hardrule-prepay-v1" for asset in registry.list_assets())
    assert store_path.exists() is True

    draft = registry.create_asset_draft(
        asset_type="risk_message_template",
        name="Injected store template",
        content={"template": "hello {rule_title}"},
    )
    reloaded_registry = AssetRegistry(store=JsonAssetStore(store_path))
    reloaded = reloaded_registry.get_asset(draft.id)

    assert reloaded.name == "Injected store template"
    assert reloaded.content_hash == draft.content_hash


def test_asset_audit_events_and_optimistic_lock(client: TestClient) -> None:
    create_response = client.post(
        "/api/assets",
        json={
            "asset_type": "risk_message_template",
            "name": "Audit lock template",
            "content": {"template": "hello {rule_title}"},
            "actor": "tester",
        },
    )
    assert create_response.status_code == 201
    draft = create_response.json()["asset"]

    stale_hash = draft["content_hash"]
    update_response = client.patch(
        f"/api/assets/{draft['id']}",
        json={
            "content": {"template": "updated {rule_title}"},
            "expected_content_hash": stale_hash,
            "actor": "tester",
        },
    )
    assert update_response.status_code == 200

    stale_update_response = client.patch(
        f"/api/assets/{draft['id']}",
        json={
            "content": {"template": "stale {rule_title}"},
            "expected_content_hash": stale_hash,
            "actor": "tester",
        },
    )
    assert stale_update_response.status_code == 400

    audit_response = client.get(f"/api/asset-audit-events?target_id={draft['id']}")
    assert audit_response.status_code == 200
    actions = [item["action"] for item in audit_response.json()["items"]]
    assert "asset.create_draft" in actions
    assert "asset.update_draft" in actions

    lock_response = client.post(
        f"/api/assets/{draft['id']}/edit-lock",
        json={"actor": "tester", "purpose": "manual_edit", "ttl_minutes": 5},
    )
    assert lock_response.status_code == 201
    lock = lock_response.json()["lock"]
    assert lock["asset_id"] == draft["id"]
    assert lock["actor"] == "tester"

    conflicting_lock_response = client.post(
        f"/api/assets/{draft['id']}/edit-lock",
        json={"actor": "other-reviewer", "purpose": "manual_edit", "ttl_minutes": 5},
    )
    assert conflicting_lock_response.status_code == 400

    locked_update_response = client.patch(
        f"/api/assets/{draft['id']}",
        json={
            "content": {"template": "blocked {rule_title}"},
            "actor": "other-reviewer",
        },
    )
    assert locked_update_response.status_code == 400

    locked_approve_response = client.post(
        f"/api/assets/{draft['id']}/approve",
        json={"actor": "other-reviewer", "comment": "try approve"},
    )
    assert locked_approve_response.status_code == 400

    same_actor_update_response = client.patch(
        f"/api/assets/{draft['id']}",
        json={
            "content": {"template": "same actor {rule_title}"},
            "actor": "tester",
        },
    )
    assert same_actor_update_response.status_code == 200

    locks_response = client.get(f"/api/asset-edit-locks?asset_id={draft['id']}")
    assert locks_response.status_code == 200
    assert locks_response.json()["total"] == 1

    audit_page_response = client.get(f"/asset-audit?target_id={draft['id']}")
    assert audit_page_response.status_code == 200
    assert "资产操作审计" in audit_page_response.text
    assert "asset.update_draft" in audit_page_response.text

    release_response = client.delete(f"/api/assets/{draft['id']}/edit-lock?actor=tester")
    assert release_response.status_code == 204
    assert client.get(f"/api/asset-edit-locks?asset_id={draft['id']}").json()["total"] == 0

    lock_audit_response = client.get(f"/api/asset-audit-events?target_id={draft['id']}")
    lock_actions = [item["action"] for item in lock_audit_response.json()["items"]]
    assert "asset.lock_acquire" in lock_actions


def test_asset_source_document_api_saves_and_splits_policy_text(client: TestClient) -> None:
    source_text = """
第一条 预付款比例
采购合同预付款原则上不得超过合同总价 25%。

第二条 例外审批
超过比例时应补充采购负责人和财务负责人例外审批。

第三条 材料处理
缺少审批材料的合同应退回补充材料。
""".strip()
    create_response = client.post(
        "/api/asset-source-documents",
        json={
            "name": "采购预付款管理制度 v1",
            "source_text": source_text,
            "source_type": "policy_document",
        },
    )

    assert create_response.status_code == 201
    document = create_response.json()["document"]
    assert document["id"].startswith("source-doc-")
    assert document["content_hash"]
    assert len(document["chunks"]) == 3
    assert document["chunks"][0]["title"].startswith("第一条")
    assert document["chunks"][0]["char_start"] == 0

    list_response = client.get("/api/asset-source-documents")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert "content_text" not in list_response.json()["items"][0]
    assert list_response.json()["items"][0]["content_preview"].startswith("第一条")

    detail_response = client.get(f"/api/asset-source-documents/{document['id']}")
    assert detail_response.status_code == 200
    assert detail_response.json()["document"]["content_text"] == source_text


def test_rule_drafts_page_imports_source_document_and_shows_chunks(client: TestClient) -> None:
    response = client.post(
        "/asset-source-documents/create",
        data={
            "name": "服务合同续约经验",
            "source_type": "experience_document",
            "source_text": "第一条 自动续约\n服务合同自动续约应保留人工确认节点。\n\n第二条 提前通知\n续约前应提前 30 日通知业务负责人。",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/rule-drafts?source_document_id=source-doc-")

    page_response = client.get(location)
    assert page_response.status_code == 200
    assert "服务合同续约经验" in page_response.text
    assert "切分结果" in page_response.text
    assert "自动续约" in page_response.text


def test_llm_rule_draft_generation_uses_source_document_chunks(client: TestClient) -> None:
    source_response = client.post(
        "/api/asset-source-documents",
        json={
            "name": "采购预付款管理制度 v2",
            "source_text": "第一条 采购合同预付款原则上不得超过合同总价 25%。\n第二条 超过比例时需要补充例外审批。",
            "source_type": "policy_document",
        },
    )
    assert source_response.status_code == 201
    document = source_response.json()["document"]

    draft_response = client.post(
        "/api/rule-drafts/generate",
        json={
            "source_document_id": document["id"],
            "profile_hint": {"contract_type": "procurement_contract"},
            "draft_types": ["policy_reference", "hard_rule", "semantic_rule", "extraction_rule"],
        },
    )

    assert draft_response.status_code == 201
    payload = draft_response.json()
    drafts = payload["drafts"]
    execution = payload["llm_execution"]
    draft_types = {draft["asset_type"] for draft in drafts}

    assert draft_types == {"policy_reference", "hard_rule", "semantic_rule", "extraction_rule"}
    assert all(draft["status"] == "draft" for draft in drafts)
    assert execution["status"] == "mock_success"
    assert execution["input_payload"]["source_document_id"] == document["id"]
    assert execution["prompt_template_id"] == "asset-prompt-rule-draft-v1"

    hard_rule = next(draft for draft in drafts if draft["asset_type"] == "hard_rule")
    assert hard_rule["content"]["conditions"][0]["value"] == 25
    assert hard_rule["content"]["source_document_id"] == document["id"]
    assert hard_rule["content"]["source_content_hash"] == document["content_hash"]
    assert hard_rule["content"]["llm_execution_id"] == execution["id"]

    executions_response = client.get("/api/llm-executions?purpose=rule_draft")
    assert executions_response.status_code == 200
    assert executions_response.json()["total"] == 1
    assert executions_response.json()["items"][0]["id"] == execution["id"]


def test_rule_drafts_page_marks_latest_generated_assets(client: TestClient) -> None:
    source_response = client.post(
        "/api/asset-source-documents",
        json={
            "name": "采购付款审批制度",
            "source_text": "第一条 采购合同预付款原则上不得超过合同总价 25%。",
            "source_type": "policy_document",
        },
    )
    document_id = source_response.json()["document"]["id"]

    response = client.post(
        "/rule-drafts/generate",
        data={
            "source_document_id": document_id,
            "source_text": "第一条 采购合同预付款原则上不得超过合同总价 25%。",
            "contract_type": "procurement_contract",
            "include_policy_reference": "true",
            "include_semantic_rule": "true",
            "include_extraction_rule": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "latest_execution_id=llm-draft-" in location

    page_response = client.get(location)
    assert page_response.status_code == 200
    assert "本次生成" in page_response.text
    assert "operation-feedback" in page_response.text
    assert "删除草稿" in page_response.text


def test_delete_controls_remove_only_safe_objects(client: TestClient) -> None:
    source_response = client.post(
        "/api/asset-source-documents",
        json={
            "name": "未引用制度",
            "source_text": "第一条 测试制度。",
            "source_type": "policy_document",
        },
    )
    document_id = source_response.json()["document"]["id"]

    delete_source_response = client.delete(f"/api/asset-source-documents/{document_id}")
    assert delete_source_response.status_code == 204
    assert client.get(f"/api/asset-source-documents/{document_id}").status_code == 404

    referenced_source_response = client.post(
        "/api/asset-source-documents",
        json={
            "name": "已引用制度",
            "source_text": "第一条 采购合同预付款原则上不得超过合同总价 25%。",
            "source_type": "policy_document",
        },
    )
    referenced_document = referenced_source_response.json()["document"]
    draft_response = client.post(
        "/api/rule-drafts/generate",
        json={
            "source_document_id": referenced_document["id"],
            "profile_hint": {"contract_type": "procurement_contract"},
            "draft_types": ["hard_rule"],
        },
    )
    draft_asset = draft_response.json()["drafts"][0]

    blocked_delete_response = client.delete(f"/api/asset-source-documents/{referenced_document['id']}")
    assert blocked_delete_response.status_code == 400

    delete_draft_response = client.delete(f"/api/assets/{draft_asset['id']}")
    assert delete_draft_response.status_code == 204
    assert client.get(f"/api/assets/{draft_asset['id']}").status_code == 404

    allowed_delete_response = client.delete(f"/api/asset-source-documents/{referenced_document['id']}")
    assert allowed_delete_response.status_code == 204


def test_semantic_rule_mock_runner_creates_trace_and_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTRACT_COMPLIANCE_LLM_DRAFT_PROVIDER", "mock")
    from app.config import get_settings
    from app.services.review_engine import analyze_contract

    get_settings.cache_clear()
    rule_context = {
        "clause_parse_templates": [
            {
                "asset_id": "asset-clause-ascii-test-v1",
                "schema_version": "clause-parse-template-v1",
                "header_pattern": r"^\[(?P<id>[A-Z]\d{3})\](?P<title>.+?)\s*$",
                "fallback": "paragraph_split",
            }
        ],
        "hard_rules": [],
        "semantic_rules": [
            {
                "asset_id": "asset-semantic-auto-renewal-v1",
                "asset_version": 1,
                "schema_version": "semantic-rule-v1",
                "name": "Auto renewal approval precondition",
                "applicability": {"contract_type": ["unknown_contract", "service_contract"]},
                "policy_reference_ids": ["POLICY-REV-004"],
            }
        ],
        "prompt_templates": [
            {
                "asset_id": "asset-prompt-semantic-rule-v1",
                "purpose": "semantic_rule",
                "schema_version": "prompt-template-v1",
            }
        ],
        "risk_evaluation_policies": [
            {
                "high": {"overall_risk": "red", "status": "pending_review"},
                "medium": {"overall_risk": "yellow", "status": "watchlist"},
                "none": {"overall_risk": "green", "status": "ready"},
            }
        ],
    }
    task = analyze_contract(
        task_id="task-semantic-test",
        source_filename="semantic-test.txt",
        contract_name="Master Service Agreement",
        contract_text=(
            "[A001] Parties\n"
            "Party A: Alpha Corp\nParty B: Beta Ltd\n\n"
            "[T001] Term\n"
            "The agreement contains automatic renewal for one year after expiry."
        ),
        rule_context=rule_context,
    )

    semantic_risks = [risk for risk in task.risks if risk.rule_version.startswith("semantic:")]
    assert len(semantic_risks) == 1
    assert semantic_risks[0].evidence_clause_ids == ["T001"]
    assert task.status == "pending_review"
    assert any(step.key == "semantic_rules" and step.status == "done" for step in task.workflow_steps)
    semantic_trace = next(event for event in task.agent_trace if event.type == "semantic.evaluate")
    assert semantic_trace.payload["hit_count"] == 1
    assert semantic_trace.payload["results"][0]["provider"] == "mock"


def test_policy_reference_titles_prefer_profile_snapshot() -> None:
    from app.models import RagflowProbe, LLMProbe, RiskFinding, TaskRecord
    from app.services.review_engine import build_review_payload

    task = TaskRecord(
        id="task-policy-test",
        name="Policy title test",
        contract_type="unknown_contract",
        contract_type_label="Unknown",
        source_filename="policy.txt",
        status="pending_review",
        status_label="Pending",
        overall_risk="red",
        overall_risk_label="High",
        decision="manual_review",
        decision_label="Manual review",
        summary="summary",
        created_at="2026-06-05T00:00:00+00:00",
        contract_text="",
        risks=[
            RiskFinding(
                rule_id="RISK-1",
                title="Risk",
                level="high",
                message="Risk",
                reason="Reason",
                evidence_clause_ids=[],
                policy_reference_ids=["POLICY-CUSTOM-001"],
                action="Action",
            )
        ],
        selected_profile_snapshot={
            "assets": [
                {
                    "asset_id": "asset-policy-custom-v1",
                    "asset_type": "policy_reference",
                    "name": "Fallback name",
                    "content": {
                        "reference_id": "POLICY-CUSTOM-001",
                        "title": "Custom policy title from asset",
                    },
                }
            ]
        },
    )

    payload = build_review_payload(
        task,
        RagflowProbe(base_url="http://ragflow.local", status="offline", healthy=False, detail="offline"),
        LLMProbe(
            configured=False,
            verified=False,
            base_url="http://llm.local",
            chat_completions_url="http://llm.local/v1/chat/completions",
            model="mock",
            status="not_configured",
            title="LLM",
            detail="not configured",
            api_key_present=False,
        ),
    )

    assert payload["risks"][0]["policy"] == "POLICY-CUSTOM-001 Custom policy title from asset"
    assert payload["risks"][0]["source"] == "hard_rule"


def test_profile_dry_run_api_does_not_create_task(client: TestClient) -> None:
    before_total = client.get("/api/tasks").json()["total"]
    sample_dir = next(path for path in (REPO_ROOT / "resource").iterdir() if path.name.startswith("01_"))
    sample_path = sorted(path for path in sample_dir.iterdir() if "B-" in path.name)[-1]
    sample_text = sample_path.read_text(encoding="utf-8")

    response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/dry-run",
        json={
            "contract_name": "dry-run procurement sample",
            "source_filename": "sample-b.md",
            "source_text": sample_text,
        },
    )

    assert response.status_code == 201
    dry_run = response.json()["dry_run"]
    assert dry_run["id"].startswith("dry-run-")
    assert dry_run["profile_id"] == "profile-procurement-basic-v1"
    assert dry_run["risk_count"] >= 1
    assert "task_snapshot" in dry_run
    assert dry_run["task_snapshot"]["id"] == dry_run["id"]
    assert client.get("/api/tasks").json()["total"] == before_total
    workflow_response = client.get(f"/api/tasks/{dry_run['id']}/workflow-run")
    assert workflow_response.status_code == 200
    workflow_run = workflow_response.json()["workflow_run"]
    assert workflow_run["run_type"] == "profile_dry_run"
    assert len(workflow_run["step_runs"]) >= 5

    list_response = client.get("/api/review-profiles/profile-procurement-basic-v1/dry-runs")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["items"][0]["id"] == dry_run["id"]


def test_profile_page_runs_dry_run_and_displays_result(client: TestClient) -> None:
    sample_dir = next(path for path in (REPO_ROOT / "resource").iterdir() if path.name.startswith("01_"))
    sample_path = sorted(path for path in sample_dir.iterdir() if "B-" in path.name)[-1]
    sample_text = sample_path.read_text(encoding="utf-8")

    response = client.post(
        "/review-profiles/profile-procurement-basic-v1/dry-run",
        data={
            "contract_name": "profile page dry-run sample",
            "source_text": sample_text,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "dry_run_id=dry-run-" in response.headers["location"]

    page_response = client.get(response.headers["location"])
    assert page_response.status_code == 200
    assert "配置集试运行结果" in page_response.text
    assert "发布前试运行" in page_response.text
    assert "profile page dry-run sample" in page_response.text


def test_publish_profile_requires_latest_dry_run(client: TestClient) -> None:
    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "Profile without dry-run"},
    )
    profile = clone_response.json()["profile"]

    publish_response = client.post(f"/api/review-profiles/{profile['id']}/publish", json={})

    assert publish_response.status_code == 400
    assert "dry-run" in publish_response.json()["detail"]


def test_publish_profile_rejects_stale_dry_run_after_binding_change(client: TestClient) -> None:
    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "Profile stale dry-run"},
    )
    profile = clone_response.json()["profile"]
    dry_run_profile_for_publish(client, profile["id"])

    bind_response = client.post(
        f"/api/review-profiles/{profile['id']}/assets",
        json={"asset_id": "asset-prompt-field-extraction-v1", "binding_reason": "change after dry-run"},
    )
    assert bind_response.status_code == 200

    publish_response = client.post(f"/api/review-profiles/{profile['id']}/publish", json={})

    assert publish_response.status_code == 400
    assert "snapshot" in publish_response.json()["detail"]


def test_risk_message_and_report_templates_drive_review_output(client: TestClient) -> None:
    message_response = client.post(
        "/api/assets",
        json={
            "asset_type": "risk_message_template",
            "name": "Template driven risk message",
            "content": {"template": "TEMPLATE::{rule_id}::{risk_level}::{reason}"},
            "schema_version": "risk-message-template-v1",
        },
    )
    report_response = client.post(
        "/api/assets",
        json={
            "asset_type": "report_template",
            "name": "Template driven report sections",
            "content": {"sections": ["summary", "policy_references", "workflow"]},
            "schema_version": "report-template-v1",
        },
    )
    message_asset = message_response.json()["asset"]
    report_asset = report_response.json()["asset"]
    for asset in [message_asset, report_asset]:
        assert client.post(f"/api/assets/{asset['id']}/approve", json={}).status_code == 200
        publish_response = client.post(f"/api/assets/{asset['id']}/publish", json={})
        assert publish_response.status_code == 200
        asset.update(publish_response.json()["asset"])

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "Template driven output profile"},
    )
    draft_profile = clone_response.json()["profile"]
    for asset in [message_asset, report_asset]:
        bind_response = client.post(
            f"/api/review-profiles/{draft_profile['id']}/assets",
            json={"asset_id": asset["id"], "binding_reason": "template output test"},
        )
        assert bind_response.status_code == 200

    dry_run_profile_for_publish(client, draft_profile["id"])
    publish_profile_response = client.post(f"/api/review-profiles/{draft_profile['id']}/publish", json={})
    assert publish_profile_response.status_code == 200
    active_profile = publish_profile_response.json()["profile"]

    sample_path = sample_contract("采购合同-样本B-收款账户不一致风险版.md")
    create_response = client.post(
        "/api/tasks",
        data={
            "contract_name": "template driven output task",
            "selected_profile_id": active_profile["id"],
        },
        files={"file": ("contract.md", sample_path.read_bytes(), "text/markdown")},
    )
    assert create_response.status_code == 201
    task_id = create_response.json()["task"]["id"]
    detail = client.get(f"/api/tasks/{task_id}").json()["task"]

    assert any(risk["message"].startswith("TEMPLATE::") for risk in detail["risks"])
    assert [section["key"] for section in detail["report"]["sections"]] == [
        "summary",
        "policy_references",
        "workflow",
    ]


def test_asset_draft_editor_updates_and_validates_hard_rule(client: TestClient) -> None:
    draft_response = client.post(
        "/api/rule-drafts/generate",
        json={
            "source_text": "采购合同预付款原则上不得超过合同总价 25%。",
            "profile_hint": {"contract_type": "procurement_contract"},
            "draft_types": ["hard_rule"],
        },
    )
    draft = draft_response.json()["drafts"][0]
    original_hash = draft["content_hash"]
    updated_content = {
        **draft["content"],
        "title": "采购合同预付款比例超过 28%",
        "conditions": [{"fact_key": "payment.prepay_ratio", "operator": ">", "value": 28}],
        "value": 28,
    }

    update_response = client.patch(
        f"/api/assets/{draft['id']}",
        json={
            "name": "采购合同预付款 28% 草稿",
            "description": "审核员修订后的草稿。",
            "applicability": {"contract_type": "procurement_contract"},
            "content": updated_content,
            "schema_version": "hard-rule-v2",
        },
    )

    assert update_response.status_code == 200
    updated = update_response.json()["asset"]
    assert updated["name"] == "采购合同预付款 28% 草稿"
    assert updated["content"]["conditions"][0]["value"] == 28
    assert updated["content_hash"] != original_hash

    approve_response = client.post(f"/api/assets/{draft['id']}/approve", json={"comment": "checked"})
    assert approve_response.status_code == 200
    assert approve_response.json()["asset"]["status"] == "approved"


def test_invalid_asset_draft_cannot_be_approved(client: TestClient) -> None:
    create_response = client.post(
        "/api/assets",
        json={
            "asset_type": "hard_rule",
            "name": "非法硬规则草稿",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {"rule_id": "BROKEN", "title": "缺少核心字段"},
        },
    )
    draft = create_response.json()["asset"]

    approve_response = client.post(f"/api/assets/{draft['id']}/approve", json={"comment": "checked"})

    assert approve_response.status_code == 400
    assert "hard_rule 草稿缺少字段" in approve_response.json()["detail"]


def test_clause_parse_template_splits_custom_numbering() -> None:
    from app.services.review_engine import parse_clauses

    contract_text = """
第1条 合同双方
甲方：星河科技有限公司
乙方：云桥科技有限公司

第2条 付款安排
甲方支付合同总价 20% 作为预付款。
""".strip()
    clauses = parse_clauses(
        contract_text,
        rule_context={
            "clause_parse_templates": [
                {
                    "asset_id": "asset-clause-article-cn-v1",
                    "schema_version": "clause-parse-template-v1",
                    "header_pattern": r"^第(?P<id>\d+)条\s+(?P<title>.+?)$",
                    "fallback": "paragraph_split",
                }
            ]
        },
    )

    assert [clause.id for clause in clauses] == ["1", "2"]
    assert clauses[0].title == "合同双方"
    assert clauses[0].parser_source == "asset-template"
    assert clauses[0].parser_template_id == "asset-clause-article-cn-v1"


def test_clause_parse_template_fallback_is_visible_in_workflow(client: TestClient) -> None:
    create_template_response = client.post(
        "/api/assets",
        json={
            "asset_type": "clause_parse_template",
            "name": "无法匹配模板",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {"header_pattern": r"^不会匹配(?P<id>\d+) (?P<title>.+)$", "fallback": "paragraph_split"},
            "schema_version": "clause-parse-template-v1",
        },
    )
    template = create_template_response.json()["asset"]
    assert client.post(f"/api/assets/{template['id']}/approve", json={}).status_code == 200
    active_template = client.post(f"/api/assets/{template['id']}/publish", json={}).json()["asset"]

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "采购合同解析 fallback 配置"},
    )
    profile = clone_response.json()["profile"]
    bind_response = client.post(
        f"/api/review-profiles/{profile['id']}/assets",
        json={"asset_id": active_template["id"], "binding_reason": "fallback parser test"},
    )
    assert bind_response.status_code == 200
    dry_run_profile_for_publish(client, profile["id"])
    publish_profile_response = client.post(f"/api/review-profiles/{profile['id']}/publish", json={})
    assert publish_profile_response.status_code == 200
    active_profile = publish_profile_response.json()["profile"]

    contract_text = """
# 办公电脑采购合同

第一段 合同双方
甲方：星河科技有限公司
乙方：云桥科技有限公司

第二段 付款安排
甲方支付合同总价 20% 作为预付款。
""".strip()
    create_task_response = client.post(
        "/api/tasks",
        data={"contract_name": "fallback parser task", "selected_profile_id": active_profile["id"]},
        files={"file": ("contract.md", contract_text.encode("utf-8"), "text/markdown")},
    )
    assert create_task_response.status_code == 201
    task_id = create_task_response.json()["task"]["id"]

    detail = client.get(f"/api/tasks/{task_id}").json()["task"]
    parsing_step = next(step for step in detail["workflow_steps"] if step["key"] == "parsing")
    parse_event = next(event for event in detail["trace"] if event["type"] == "document.parse")

    assert parsing_step["status"] == "warning"
    assert parse_event["payload"]["fallback_used"] is True
    assert parse_event["payload"]["parser_template_id"] == active_template["id"]

    clauses_response = client.get(f"/api/tasks/{task_id}/clauses")
    assert clauses_response.status_code == 200
    assert clauses_response.json()["items"][0]["parser_source"] == "fallback:paragraph_split"
    assert clauses_response.json()["items"][0]["chunk_id"] == active_template["id"]


def test_extraction_schema_controls_displayed_fields(client: TestClient) -> None:
    schema_response = client.post(
        "/api/assets",
        json={
            "asset_type": "extraction_schema",
            "name": "Minimal procurement field schema",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {
                "fields": [
                    {"key": "contract_type", "label": "合同类型"},
                    {"key": "payment.prepay_ratio", "label": "预付款比例"},
                    {"key": "invoice.tax_rate", "label": "税率"},
                ]
            },
        },
    )
    assert schema_response.status_code == 201
    draft_schema = schema_response.json()["asset"]
    assert client.post(f"/api/assets/{draft_schema['id']}/approve", json={"comment": "checked"}).status_code == 200
    publish_schema_response = client.post(f"/api/assets/{draft_schema['id']}/publish", json={})
    assert publish_schema_response.status_code == 200
    active_schema = publish_schema_response.json()["asset"]

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "Schema driven procurement profile"},
    )
    draft_profile = clone_response.json()["profile"]
    bind_response = client.post(
        f"/api/review-profiles/{draft_profile['id']}/assets",
        json={"asset_id": active_schema["id"], "binding_reason": "display selected fields only"},
    )
    assert bind_response.status_code == 200
    dry_run_profile_for_publish(client, draft_profile["id"])
    publish_profile_response = client.post(f"/api/review-profiles/{draft_profile['id']}/publish", json={})
    active_profile = publish_profile_response.json()["profile"]

    contract_text = """
# 办公电脑采购合同

【A001】合同双方
甲方：星河科技有限公司
乙方：上海云桥科技有限公司

【A002】付款方式
甲方支付合同总价 40% 作为预付款，剩余 60% 在到货验收后支付。

【A003】发票
乙方应开具增值税专用发票，税率 13%。
""".strip()
    create_response = client.post(
        "/api/tasks",
        data={"contract_name": "schema display check", "selected_profile_id": active_profile["id"]},
        files={"file": ("contract.md", contract_text.encode("utf-8"), "text/markdown")},
    )
    assert create_response.status_code == 201
    task_id = create_response.json()["task"]["id"]
    fields = client.get(f"/api/tasks/{task_id}/facts").json()["items"]

    assert [item["fact_key"] for item in fields] == [
        "contract_type",
        "payment.prepay_ratio",
        "invoice.tax_rate",
    ]
    assert next(item for item in fields if item["fact_key"] == "payment.prepay_ratio")["value"] == "40%"


def test_extraction_rule_adds_configured_fact(client: TestClient) -> None:
    schema_response = client.post(
        "/api/assets",
        json={
            "asset_type": "extraction_schema",
            "name": "Approval field schema",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {
                "fields": [
                    "contract_type",
                    {"key": "approval.record_no", "label": "例外审批编号"},
                    "payment.prepay_ratio",
                ]
            },
        },
    )
    extraction_rule_response = client.post(
        "/api/assets",
        json={
            "asset_type": "extraction_rule",
            "name": "Approval record extractor",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {
                "fact_key": "approval.record_no",
                "label": "例外审批编号",
                "regex": r"例外审批编号[:：]\s*(?P<value>[A-Z]+-\d+)",
                "value_type": "text",
            },
        },
    )
    assert schema_response.status_code == 201
    assert extraction_rule_response.status_code == 201
    draft_schema = schema_response.json()["asset"]
    draft_rule = extraction_rule_response.json()["asset"]
    assert client.post(f"/api/assets/{draft_schema['id']}/approve", json={}).status_code == 200
    assert client.post(f"/api/assets/{draft_rule['id']}/approve", json={}).status_code == 200
    active_schema = client.post(f"/api/assets/{draft_schema['id']}/publish", json={}).json()["asset"]
    active_rule = client.post(f"/api/assets/{draft_rule['id']}/publish", json={}).json()["asset"]

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "Schema and extraction rule profile"},
    )
    draft_profile = clone_response.json()["profile"]
    for asset in [active_schema, active_rule]:
        assert (
            client.post(
                f"/api/review-profiles/{draft_profile['id']}/assets",
                json={"asset_id": asset["id"], "binding_reason": "configured extraction test"},
            ).status_code
            == 200
        )
    dry_run_profile_for_publish(client, draft_profile["id"])
    active_profile = client.post(f"/api/review-profiles/{draft_profile['id']}/publish", json={}).json()["profile"]

    contract_text = """
# 办公电脑采购合同

【A001】合同双方
甲方：星河科技有限公司
乙方：上海云桥科技有限公司

【A002】付款方式
甲方支付合同总价 45% 作为预付款。例外审批编号：APR-202606。
""".strip()
    create_response = client.post(
        "/api/tasks",
        data={"contract_name": "configured extraction check", "selected_profile_id": active_profile["id"]},
        files={"file": ("contract.md", contract_text.encode("utf-8"), "text/markdown")},
    )
    assert create_response.status_code == 201
    task_id = create_response.json()["task"]["id"]
    fields = client.get(f"/api/tasks/{task_id}/facts").json()["items"]
    approval_field = next(item for item in fields if item["fact_key"] == "approval.record_no")

    assert approval_field["label"] == "例外审批编号"
    assert approval_field["value"] == "APR-202606"
    assert approval_field["status"] == "present"
    assert approval_field["evidence_clause_ids"]


def test_llm_field_extraction_fallback_marks_candidate_field(client: TestClient) -> None:
    schema_response = client.post(
        "/api/assets",
        json={
            "asset_type": "extraction_schema",
            "name": "LLM fallback approval field schema",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {
                "fields": [
                    "contract_type",
                    {"key": "approval.record_no", "label": "例外审批编号"},
                    "payment.prepay_ratio",
                ]
            },
        },
    )
    assert schema_response.status_code == 201
    draft_schema = schema_response.json()["asset"]
    assert client.post(f"/api/assets/{draft_schema['id']}/approve", json={}).status_code == 200
    active_schema = client.post(f"/api/assets/{draft_schema['id']}/publish", json={}).json()["asset"]

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "LLM fallback field extraction profile"},
    )
    draft_profile = clone_response.json()["profile"]
    for asset_id in [active_schema["id"], "asset-prompt-field-extraction-v1"]:
        assert (
            client.post(
                f"/api/review-profiles/{draft_profile['id']}/assets",
                json={"asset_id": asset_id, "binding_reason": "field fallback test"},
            ).status_code
            == 200
        )
    dry_run_profile_for_publish(client, draft_profile["id"])
    active_profile = client.post(f"/api/review-profiles/{draft_profile['id']}/publish", json={}).json()["profile"]

    contract_text = """
# 办公电脑采购合同

【A001】合同双方
甲方：星河科技有限公司
乙方：上海云桥科技有限公司

【A002】付款方式
甲方支付合同总价 45% 作为预付款。例外审批编号：APR-202606。
""".strip()
    create_response = client.post(
        "/api/tasks",
        data={"contract_name": "llm fallback extraction check", "selected_profile_id": active_profile["id"]},
        files={"file": ("contract.md", contract_text.encode("utf-8"), "text/markdown")},
    )
    assert create_response.status_code == 201
    task_id = create_response.json()["task"]["id"]
    fields = client.get(f"/api/tasks/{task_id}/facts").json()["items"]
    detail = client.get(f"/api/tasks/{task_id}").json()["task"]
    approval_field = next(item for item in fields if item["fact_key"] == "approval.record_no")
    fact_event = next(event for event in detail["trace"] if event["type"] == "fact.extract")

    assert approval_field["value"] == "APR-202606"
    assert approval_field["status"] == "candidate"
    assert approval_field["evidence_clause_ids"]
    assert fact_event["payload"]["llm_candidate_count"] == 1
    assert fact_event["payload"]["llm_candidate_fields"] == ["approval.record_no"]


def test_hard_rule_condition_tree_supports_all_any_not() -> None:
    from app.services.review_engine import evaluate_hard_rule, set_fact

    facts = {}
    set_fact(facts, "payment.prepay_ratio", "45%", ["C002"])
    set_fact(facts, "invoice.tax_rate", "13%", ["C003"])
    set_fact(facts, "approval.record_no", None, [])
    rule = {
        "rule_id": "DSL-PUR-001",
        "title": "DSL condition tree rule",
        "level": "high",
        "applicability": {"contract_type": "procurement_contract"},
        "condition_tree": {
            "all": [
                {
                    "any": [
                        {"fact_key": "payment.prepay_ratio", "operator": ">", "value": 40},
                        {"fact_key": "invoice.tax_rate", "operator": "missing"},
                    ]
                },
                {"not": {"fact_key": "approval.record_no", "operator": "present"}},
            ]
        },
        "policy_reference_ids": ["POLICY-DSL-001"],
        "reason_template": "prepay {payment.prepay_ratio}",
        "action_template": "manual review",
    }

    risk = evaluate_hard_rule(rule, "procurement_contract", facts)

    assert risk is not None
    assert risk.rule_id == "DSL-PUR-001"
    assert risk.evidence_clause_ids == ["C002", "C003"]


def test_hard_rule_condition_tree_asset_affects_review(client: TestClient) -> None:
    create_rule_response = client.post(
        "/api/assets",
        json={
            "asset_type": "hard_rule",
            "name": "DSL prepay or missing tax rule",
            "applicability": {"contract_type": "procurement_contract"},
            "content": {
                "rule_id": "DSL-PUR-001",
                "title": "DSL 高预付或缺税率且无审批",
                "level": "high",
                "condition_tree": {
                    "all": [
                        {
                            "any": [
                                {"fact_key": "payment.prepay_ratio", "operator": ">", "value": 40},
                                {"fact_key": "invoice.tax_rate", "operator": "missing"},
                            ]
                        },
                        {"not": {"fact_key": "approval.record_no", "operator": "present"}},
                    ]
                },
                "evidence_fact_keys": [],
                "policy_reference_ids": ["POLICY-DSL-001"],
                "reason_template": "合同存在高预付或缺税率，且未看到例外审批编号。",
                "action_template": "请补充审批依据或调整付款/发票条款。",
            },
            "schema_version": "hard-rule-v3",
        },
    )
    assert create_rule_response.status_code == 201
    draft_rule = create_rule_response.json()["asset"]
    assert client.post(f"/api/assets/{draft_rule['id']}/approve", json={"comment": "dsl checked"}).status_code == 200
    active_rule = client.post(f"/api/assets/{draft_rule['id']}/publish", json={}).json()["asset"]

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "DSL hard rule profile"},
    )
    draft_profile = clone_response.json()["profile"]
    bind_response = client.post(
        f"/api/review-profiles/{draft_profile['id']}/assets",
        json={"asset_id": active_rule["id"], "binding_reason": "dsl hard rule test"},
    )
    assert bind_response.status_code == 200
    dry_run_profile_for_publish(client, draft_profile["id"])
    active_profile = client.post(f"/api/review-profiles/{draft_profile['id']}/publish", json={}).json()["profile"]

    contract_text = """
# 办公电脑采购合同

【A001】合同双方
甲方：星河科技有限公司
乙方：上海云桥科技有限公司

【A002】付款方式
甲方支付合同总价 45% 作为预付款，剩余 55% 在到货验收后支付。

【A003】发票
乙方应开具增值税专用发票，税率 13%。
""".strip()
    create_task_response = client.post(
        "/api/tasks",
        data={"contract_name": "dsl hard rule task", "selected_profile_id": active_profile["id"]},
        files={"file": ("contract.md", contract_text.encode("utf-8"), "text/markdown")},
    )
    assert create_task_response.status_code == 201
    task_id = create_task_response.json()["task"]["id"]
    detail = client.get(f"/api/tasks/{task_id}").json()["task"]
    rule_ids = {item["rule"] for item in detail["risks"]}

    assert "DSL-PUR-001" in rule_ids


def test_rule_drafts_editor_reports_invalid_json(client: TestClient) -> None:
    draft_response = client.post(
        "/api/rule-drafts/generate",
        json={
            "source_text": "采购合同预付款原则上不得超过合同总价 25%。",
            "profile_hint": {"contract_type": "procurement_contract"},
            "draft_types": ["hard_rule"],
        },
    )
    draft = draft_response.json()["drafts"][0]

    response = client.post(
        f"/assets/{draft['id']}/update-draft",
        data={
            "name": draft["name"],
            "schema_version": draft["schema_version"],
            "description": draft["description"] or "",
            "applicability_json": '{"contract_type": "procurement_contract"}',
            "content_json": "{bad-json",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "JSON 格式错误" in response.text


def test_llm_rule_draft_generation_rejects_invalid_structured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTRACT_COMPLIANCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTRACT_COMPLIANCE_BOOTSTRAP_SAMPLES", "0")
    monkeypatch.setenv("CONTRACT_COMPLIANCE_TASK_STORE_BACKEND", "json")
    monkeypatch.setenv("CONTRACT_COMPLIANCE_OBJECT_STORAGE", "local")
    monkeypatch.setenv("RAGFLOW_BASE_URL", "http://127.0.0.1:65530")
    monkeypatch.setenv("LLM_API_KEY", "test-secret-value")
    monkeypatch.setenv("LLM_PROBE_ENABLED", "0")
    monkeypatch.setenv("CONTRACT_COMPLIANCE_LLM_DRAFT_PROVIDER", "invalid_mock")

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as invalid_client:
        response = invalid_client.post(
            "/api/rule-drafts/generate",
            json={
                "source_text": "采购合同预付款不得超过 25%。",
                "draft_types": ["hard_rule"],
            },
        )
        assert response.status_code == 400
        assert "结构化校验失败" in response.json()["detail"]

        executions_response = invalid_client.get("/api/llm-executions?purpose=rule_draft")
        assert executions_response.status_code == 200
        executions = executions_response.json()["items"]
        assert executions[0]["status"] == "validation_error"
        assert executions[0]["error_detail"]
    get_settings.cache_clear()


def test_llm_rule_draft_validation_normalizes_common_model_variants(client: TestClient) -> None:
    from app.services.assets import AssetRegistry

    registry = AssetRegistry()
    normalized = registry._validate_rule_draft_payload(  # noqa: SLF001
        {
            "drafts": [
                {
                    "asset_type": "hard_rule",
                    "name": "采购合同超预算支出审批规则",
                    "applicability": "procurement_contract",
                    "schema_version": "rule-draft-v1",
                    "content": {
                        "rule_id": "HR-PROC-001",
                        "title": "采购合同超预算支出审批规则",
                        "level": "mandatory",
                        "conditions": {
                            "trigger_condition": "采购合同执行过程中发生预算外支出或超预算支出",
                            "threshold": [
                                {"field": "单笔支出金额", "operator": ">", "value": 0, "unit": "元"}
                            ],
                        },
                        "evidence_fact_key": ["超预算申请说明", "总经理审批意见"],
                        "policy_reference_ids": "财务管理制度_货币资金管理_授权审批规定",
                        "reason_template": "超预算支出须有充分合理的说明和审批手续。",
                        "action_template": "提交超预算申请并完成审批。",
                        "source_chunks": ["source-doc-demo-chunk-001"],
                    },
                }
            ]
        },
        selected_types=["hard_rule"],
    )

    hard_rule = normalized[0]
    content = hard_rule["content"]

    assert hard_rule["applicability"] == {"contract_type": "procurement_contract"}
    assert content["level"] == "high"
    assert content["conditions"] == [{"fact_key": "单笔支出金额", "operator": ">", "value": 0}]
    assert content["evidence_fact_keys"] == ["超预算申请说明", "总经理审批意见"]
    assert content["policy_reference_ids"] == ["财务管理制度_货币资金管理_授权审批规定"]
    assert content["source_chunk_ids"] == ["source-doc-demo-chunk-001"]


def test_review_profiles_and_assets_are_seeded(client: TestClient) -> None:
    profiles_response = client.get("/api/review-profiles")
    assets_response = client.get("/api/assets")

    assert profiles_response.status_code == 200
    assert assets_response.status_code == 200
    profiles = profiles_response.json()["items"]
    assets = assets_response.json()["items"]

    assert any(item["id"] == "profile-procurement-basic-v1" for item in profiles)
    assert any(item["asset_type"] == "hard_rule" for item in assets)
    assert any(item["asset_type"] == "report_template" for item in assets)
    assert all("execution_status" in item for item in assets)


def test_asset_execution_audit_reports_real_runtime_coverage(client: TestClient) -> None:
    audit_response = client.get("/api/assets/execution-audit")
    profile_response = client.get("/api/review-profiles/profile-procurement-basic-v1")

    assert audit_response.status_code == 200
    assert profile_response.status_code == 200

    audit_items = {item["asset_type"]: item for item in audit_response.json()["items"]}
    assert audit_items["hard_rule"]["status"] == "implemented"
    assert audit_items["hard_rule"]["active_assets"] >= 1
    assert audit_items["extraction_schema"]["status"] == "partially_implemented"
    assert audit_items["semantic_rule"]["status"] == "implemented"

    profile_audit = profile_response.json()["execution_audit"]
    assert profile_audit["summary"]["implemented"] >= 1
    assert any(item["asset_type"] == "hard_rule" for item in profile_audit["items"])


def test_asset_versions_are_copied_as_draft_with_content_hash(client: TestClient) -> None:
    source_response = client.get("/api/assets/asset-hardrule-prepay-v1")
    assert source_response.status_code == 200
    source_asset = source_response.json()["asset"]
    source_hash = source_asset["content_hash"]

    clone_response = client.post(
        "/api/assets/asset-hardrule-prepay-v1/versions",
        json={"name": "Prepay threshold next version"},
    )
    assert clone_response.status_code == 201
    cloned_asset = clone_response.json()["asset"]

    assert cloned_asset["status"] == "draft"
    assert cloned_asset["parent_asset_id"] == "asset-hardrule-prepay-v1"
    assert cloned_asset["version"] == source_asset["version"] + 1
    assert cloned_asset["content_hash"] == source_hash

    unchanged_source = client.get("/api/assets/asset-hardrule-prepay-v1").json()["asset"]
    assert unchanged_source["status"] == "active"
    assert unchanged_source["content_hash"] == source_hash


def test_asset_management_closes_profile_usage_loop(client: TestClient) -> None:
    draft_response = client.post(
        "/api/rule-drafts/generate",
        json={
            "source_text": "采购合同预付款原则上不得超过合同总价 60%，超过时需要补充例外审批。",
            "profile_hint": {"contract_type": "procurement_contract"},
            "draft_types": ["hard_rule"],
        },
    )
    assert draft_response.status_code == 201
    draft_asset = draft_response.json()["drafts"][0]
    assert draft_asset["status"] == "draft"
    assert draft_asset["content"]["value"] == 60

    approve_response = client.post(
        f"/api/assets/{draft_asset['id']}/approve",
        json={"comment": "policy checked"},
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["asset"]["status"] == "approved"

    publish_response = client.post(f"/api/assets/{draft_asset['id']}/publish", json={})
    assert publish_response.status_code == 200
    active_asset = publish_response.json()["asset"]
    assert active_asset["status"] == "active"

    clone_response = client.post(
        "/api/review-profiles/profile-procurement-basic-v1/versions",
        json={"name": "Procurement threshold 60"},
    )
    assert clone_response.status_code == 201
    draft_profile = clone_response.json()["profile"]
    assert draft_profile["status"] == "draft"

    bind_response = client.post(
        f"/api/review-profiles/{draft_profile['id']}/assets",
        json={"asset_id": active_asset["id"], "binding_reason": "threshold policy update"},
    )
    assert bind_response.status_code == 200
    assert bind_response.json()["asset_counts"]["hard_rule"] == 7

    dry_run_profile_for_publish(client, draft_profile["id"])
    profile_publish_response = client.post(
        f"/api/review-profiles/{draft_profile['id']}/publish",
        json={"comment": "ready for upload"},
    )
    assert profile_publish_response.status_code == 200
    active_profile = profile_publish_response.json()["profile"]
    assert active_profile["status"] == "active"

    contract_text = """
# 办公电脑采购合同

【A001】合同双方
甲方：星河科技有限公司
乙方：上海云桥科技有限公司

【A002】合同金额
合同总价为人民币 100000 元。

【A003】付款方式
甲方支付合同总价 50% 作为预付款，剩余 50% 在到货验收后支付。

【A004】发票
乙方应开具增值税专用发票，税率 13%。

【A005】验收
货物到货后由甲方验收确认。

【A006】收款账户
账户名称：上海云桥科技有限公司第三方账户
""".strip()
    create_response = client.post(
        "/api/tasks",
        data={
            "contract_name": "threshold usage check",
            "selected_profile_id": active_profile["id"],
        },
        files={"file": ("contract.md", contract_text.encode("utf-8"), "text/markdown")},
    )
    assert create_response.status_code == 201
    task_id = create_response.json()["task"]["id"]
    detail_response = client.get(f"/api/tasks/{task_id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()["task"]
    rule_ids = {item["rule"] for item in detail["risks"]}
    snapshot_asset_ids = {item["asset_id"] for item in detail["profile"]["assets"]}
    snapshot_hashes = {item["asset_id"]: item.get("content_hash") for item in detail["profile"]["assets"]}

    assert active_asset["id"] in snapshot_asset_ids
    assert snapshot_hashes[active_asset["id"]] == active_asset["content_hash"]
    assert "FIN-PUR-003" not in rule_ids
    assert "FIN-PUR-005" in rule_ids


def test_create_task_and_fetch_review_payload(client: TestClient) -> None:
    sample_path = sample_contract("采购合同-样本B-收款账户不一致风险版.md")
    payload = sample_path.read_bytes()

    create_response = client.post(
        "/api/tasks",
        data={
            "contract_name": "办公电脑采购合同",
            "selected_profile_id": "profile-procurement-basic-v1",
        },
        files={"file": ("contract.md", payload, "text/markdown")},
    )

    assert create_response.status_code == 201
    task = create_response.json()["task"]
    assert task["overall_risk"] == "red"
    assert task["selected_profile_id"] == "profile-procurement-basic-v1"

    detail_response = client.get(f"/api/tasks/{task['id']}")
    assert detail_response.status_code == 200
    review_task = detail_response.json()["task"]
    rule_ids = {item["rule"] for item in review_task["risks"]}

    assert "FIN-PUR-003" in rule_ids
    assert "FIN-PUR-005" in rule_ids
    assert review_task["task"]["risk"] == "高风险"
    assert review_task["profile"]["id"] == "profile-procurement-basic-v1"
    assert review_task["profile"]["hard_rule_count"] >= 1
    assert len(review_task["workflow_steps"]) >= 5
    assert review_task["workflow_run"]["status"] in {"succeeded", "waiting_human", "succeeded_with_warnings"}
    assert review_task["workflow_run"]["source"] == "analyze_contract.v2.executor"
    assert review_task["workflow_run"]["checkpoint_count"] >= 5
    assert len(review_task["workflow_run"]["step_runs"]) >= 5
    assert all(step["checkpoint_saved"] for step in review_task["workflow_run"]["step_runs"])
    assert any(event["type"] == "rule.evaluate" for event in review_task["trace"])
    assert review_task["report"]["summary"]

    clauses_response = client.get(f"/api/tasks/{task['id']}/clauses")
    facts_response = client.get(f"/api/tasks/{task['id']}/facts")
    rule_hits_response = client.get(f"/api/tasks/{task['id']}/rule-hits")
    reports_response = client.get(f"/api/tasks/{task['id']}/report-snapshots")
    workflow_response = client.get(f"/api/tasks/{task['id']}/workflow-run")

    assert clauses_response.status_code == 200
    assert facts_response.status_code == 200
    assert rule_hits_response.status_code == 200
    assert reports_response.status_code == 200
    assert workflow_response.status_code == 200
    assert clauses_response.json()["items"][0]["clause_id"]
    assert any(item["fact_key"] == "payment.prepay_ratio" for item in facts_response.json()["items"])
    assert any(item["rule_id"] == "FIN-PUR-003" for item in rule_hits_response.json()["items"])

    retry_response = client.post(f"/api/tasks/{task['id']}/workflow-run/steps/parsing/retry")
    assert retry_response.status_code == 200
    retry_payload = retry_response.json()
    parsing_step = next(
        item for item in retry_payload["workflow_run"]["step_runs"] if item["step_key"] == "parsing"
    )
    assert parsing_step["retry_count"] == 1
    assert parsing_step["metadata"]["checkpoint_saved"] is True
    assert retry_payload["workflow_run"]["source"] == "analyze_contract.v2.executor.retry"
    assert any(event["type"] == "workflow.retry" for event in retry_payload["task"]["trace"])

    resume_response = client.post(f"/api/tasks/{task['id']}/workflow-run/resume?resume_from_step=evaluating")
    assert resume_response.status_code == 200
    resume_payload = resume_response.json()
    assert resume_payload["workflow_run"]["source"] == "analyze_contract.v2.executor.resume"
    assert resume_payload["workflow_run"]["metadata"]["checkpoint_count"] >= 5
    assert resume_payload["workflow_run"]["metadata"]["resume_from_step"] == "evaluating"
    assert set(resume_payload["workflow_run"]["metadata"]["reused_checkpoint_steps"]) == {
        "uploaded",
        "parsing",
        "extracting",
    }
    execution_plan = {
        item["step_key"]: item for item in resume_payload["workflow_run"]["metadata"]["execution_plan"]
    }
    assert execution_plan["parsing"]["action"] == "reuse_checkpoint"
    assert execution_plan["parsing"]["status"] == "skipped"
    assert execution_plan["evaluating"]["action"] == "execute"
    resumed_parsing_step = next(
        item for item in resume_payload["workflow_run"]["step_runs"] if item["step_key"] == "parsing"
    )
    resumed_evaluating_step = next(
        item for item in resume_payload["workflow_run"]["step_runs"] if item["step_key"] == "evaluating"
    )
    assert resumed_parsing_step["status"] == "skipped"
    assert resumed_parsing_step["metadata"]["execution_mode"] == "checkpoint_reused"
    assert resumed_parsing_step["metadata"]["reused_checkpoint"] is True
    assert resumed_parsing_step["metadata"]["worker_status"] == "skipped_by_checkpoint"
    assert resumed_parsing_step["metadata"]["physical_skip"] is True
    assert resumed_evaluating_step["metadata"]["execution_mode"] == "resumed_execution"
    assert resumed_evaluating_step["metadata"]["reused_checkpoint"] is False
    assert resumed_evaluating_step["metadata"]["worker_status"] == "executed_inline"
    assert any(event["type"] == "workflow.resume" for event in resume_payload["task"]["trace"])

    workflow_status_response = client.get(f"/api/tasks/{task['id']}/workflow-run/status")
    assert workflow_status_response.status_code == 200
    workflow_status = workflow_status_response.json()["workflow_status"]
    assert workflow_status["worker_mode"] == "inline_plan_executor"
    assert workflow_status["worker_status"] == "completed"
    assert workflow_status["progress"]["percent"] == 100
    status_results = {item["step_key"]: item for item in workflow_status["worker_results"]}
    assert status_results["parsing"]["worker_status"] == "skipped_by_checkpoint"
    assert status_results["evaluating"]["worker_status"] == "executed_inline"

    queue_response = client.post(f"/api/tasks/{task['id']}/workflow-run/queue")
    assert queue_response.status_code == 200
    queued_run = queue_response.json()["workflow_run"]
    assert queued_run["source"] == "analyze_contract.v3.async_worker"
    assert queued_run["status"] == "queued"
    assert queued_run["metadata"]["worker_mode"] == "async_plan_worker"
    assert queued_run["metadata"]["worker_status"] == "queued"

    start_response = client.post(f"/api/tasks/{task['id']}/workflow-run/worker/start")
    assert start_response.status_code == 200
    assert start_response.json()["workflow_run"]["metadata"]["worker_status"] == "running"

    pause_response = client.post(f"/api/tasks/{task['id']}/workflow-run/worker/pause")
    assert pause_response.status_code == 200
    assert pause_response.json()["workflow_run"]["metadata"]["worker_status"] == "paused"

    worker_resume_response = client.post(f"/api/tasks/{task['id']}/workflow-run/worker/resume")
    assert worker_resume_response.status_code == 200
    assert worker_resume_response.json()["workflow_run"]["metadata"]["worker_status"] in {"running", "completed"}

    completed_run = None
    for _ in range(20):
        polled_status = client.get(f"/api/tasks/{task['id']}/workflow-run/status")
        assert polled_status.status_code == 200
        workflow_status = polled_status.json()["workflow_status"]
        if workflow_status["worker_status"] == "completed":
            completed_run = client.get(f"/api/tasks/{task['id']}/workflow-run").json()["workflow_run"]
            break
        time.sleep(0.05)
    assert completed_run is not None
    assert completed_run["status"] != "running"
    assert completed_run["metadata"]["worker_status"] == "completed"
    completed_results = {item["step_key"]: item for item in completed_run["metadata"]["worker_results"]}
    assert completed_results["parsing"]["worker_status"] == "skipped_by_checkpoint"
    assert completed_results["evaluating"]["worker_status"] == "executed_by_background_worker"
    assert completed_results["parsing"]["artifact_reused"] is True

    report_item = reports_response.json()["items"][0]
    assert report_item["file_path"]
    assert Path(report_item["file_path"]).exists()
    assert report_item["source_file_sha256"]

    review_action_response = client.post(
        f"/api/tasks/{task['id']}/review-actions",
        json={
            "target_type": "rule_hit",
            "target_id": "FIN-PUR-003",
            "action_type": "request_evidence",
            "comment": "Need approval evidence.",
        },
    )
    assert review_action_response.status_code == 201
    blocked_resume_response = client.post(f"/api/tasks/{task['id']}/workflow-run/resume")
    assert blocked_resume_response.status_code == 400

    actions_response = client.get(f"/api/tasks/{task['id']}/review-actions")
    updated_hits_response = client.get(f"/api/tasks/{task['id']}/rule-hits")
    updated_hits = updated_hits_response.json()["items"]

    assert actions_response.json()["items"][0]["action_type"] == "request_evidence"
    assert next(item for item in updated_hits if item["rule_id"] == "FIN-PUR-003")["review_status"] == "evidence_requested"

    blocked_retry_response = client.post(f"/api/tasks/{task['id']}/workflow-run/steps/parsing/retry")
    assert blocked_retry_response.status_code == 400

    updated_reports_response = client.get(f"/api/tasks/{task['id']}/report-snapshots")
    updated_reports = updated_reports_response.json()["items"]
    updated_detail_response = client.get(f"/api/tasks/{task['id']}")
    updated_detail = updated_detail_response.json()["task"]

    assert updated_reports[0]["version"] == 2
    assert updated_reports[0]["file_sha256"]
    assert updated_detail["report"]["version"] == 2
    assert next(item for item in updated_detail["risks"] if item["rule"] == "FIN-PUR-003")["review_status_label"] == "待补证据"

    task_decision_response = client.post(
        f"/api/tasks/{task['id']}/task-decisions",
        json={
            "action_type": "return_materials",
            "comment": "Business owner must provide approval evidence.",
        },
    )
    assert task_decision_response.status_code == 201

    finalized_detail_response = client.get(f"/api/tasks/{task['id']}")
    finalized_detail = finalized_detail_response.json()["task"]
    finalized_actions_response = client.get(f"/api/tasks/{task['id']}/review-actions")

    assert finalized_detail["report"]["version"] == 3
    assert finalized_detail["task_decision"]["latest_comment"] == "Business owner must provide approval evidence."
    finalized_actions = finalized_actions_response.json()["items"]
    assert any(item["target_type"] == "task" for item in finalized_actions)
    assert any(item["action_type"] == "return_materials" for item in finalized_actions)

    generated_report_response = client.post(
        f"/api/tasks/{task['id']}/reports",
        json={
            "comment": "Generate delivery report for archive.",
        },
    )
    assert generated_report_response.status_code == 201
    generated_report = generated_report_response.json()["report"]

    assert generated_report["version"] == 4
    assert generated_report["report_type"] == "delivery_report"
    assert generated_report["file_sha256"]

    report_markdown_response = client.get(f"/api/tasks/{task['id']}/reports/{generated_report['version']}")
    assert report_markdown_response.status_code == 200
    assert "Report type: 交付报告 (delivery_report)" in report_markdown_response.text
    assert "## Review actions" in report_markdown_response.text


def test_rejects_unsupported_upload_type(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        data={"contract_name": "bad file", "selected_profile_id": "profile-procurement-basic-v1"},
        files={"file": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 400
    assert ".md" in response.json()["detail"]


def test_rejects_upload_without_review_profile(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        data={"contract_name": "missing profile"},
        files={"file": ("contract.md", b"# contract", "text/markdown")},
    )

    assert response.status_code == 400
    assert "配置集" in response.json()["detail"]


def test_dashboard_and_review_pages_render_created_task(client: TestClient) -> None:
    sample_path = sample_contract("服务合同-样本B-高预付款风险版.md")
    payload = sample_path.read_bytes()

    response = client.post(
        "/tasks/create",
        data={
            "contract_name": "营销系统开发服务合同",
            "selected_profile_id": "profile-service-basic-v1",
        },
        files={"file": ("service.md", payload, "text/markdown")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    review_url = response.headers["location"]

    dashboard_response = client.get("/")
    assert dashboard_response.status_code == 200
    assert "营销系统开发服务合同" in dashboard_response.text
    assert "服务合同基础审查" in dashboard_response.text

    review_api_response = client.get("/api/tasks")
    task_id = review_api_response.json()["items"][0]["id"]
    detail_response = client.get(f"/api/tasks/{task_id}")
    rule_ids = {item["rule"] for item in detail_response.json()["task"]["risks"]}

    assert "FIN-SVC-004" in rule_ids
    assert "FIN-SVC-006" in rule_ids

    review_response = client.get(review_url)
    assert review_response.status_code == 200
    assert "审查配置集" in review_response.text
    assert "/task-decision" in review_response.text
    assert "/reports" in review_response.text
    assert "RAGFlow 状态" in review_response.text
    assert "模型服务" in review_response.text
    assert "高风险" in review_response.text
    assert "Agent 决策轨迹" in review_response.text
    assert "报告快照" in review_response.text


def test_system_status_does_not_leak_llm_secret(client: TestClient) -> None:
    response = client.get("/api/system/status")

    assert response.status_code == 200
    payload_text = response.text
    payload = response.json()

    assert "test-secret-value" not in payload_text
    assert payload["llm"]["api_key_present"] is True
    assert payload["llm"]["status"] == "configured"


def test_dashboard_shows_manual_llm_probe_panel(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "大模型可用性检查" in response.text
    assert "主动检测模型" in response.text


def test_asset_workbench_pages_render(client: TestClient) -> None:
    assets_response = client.get("/assets")
    drafts_response = client.get("/rule-drafts")
    profile_response = client.get("/review-profiles/profile-procurement-basic-v1")

    assert assets_response.status_code == 200
    assert drafts_response.status_code == 200
    assert profile_response.status_code == 200
    assert "/rule-drafts" in assets_response.text
    assert "资产执行状态审计" in assets_response.text
    assert "已接入执行" in assets_response.text
    assert "/assets" in drafts_response.text
    assert "profile-procurement-basic-v1" in profile_response.text
    assert "执行状态" in profile_response.text


def test_manual_llm_check_returns_probe_result(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.services import llm

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"OK"}}]}'

    def fake_urlopen(req, timeout):  # noqa: ANN001
        assert req.full_url == "https://gen.trendbot.cn/v1/chat/completions"
        assert timeout > 0
        return FakeResponse()

    get_settings.cache_clear()
    monkeypatch.setattr(llm.request, "urlopen", fake_urlopen)

    response = client.post("/api/llm/check")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "verified"
    assert payload["response_preview"] == "OK"
    assert payload["latency_ms"] is not None
    get_settings.cache_clear()
