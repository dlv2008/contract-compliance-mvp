from pathlib import Path
import sys

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
    assert audit_items["semantic_rule"]["status"] == "partially_implemented"

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
    assert any(event["type"] == "rule.evaluate" for event in review_task["trace"])
    assert review_task["report"]["summary"]

    clauses_response = client.get(f"/api/tasks/{task['id']}/clauses")
    facts_response = client.get(f"/api/tasks/{task['id']}/facts")
    rule_hits_response = client.get(f"/api/tasks/{task['id']}/rule-hits")
    reports_response = client.get(f"/api/tasks/{task['id']}/report-snapshots")

    assert clauses_response.status_code == 200
    assert facts_response.status_code == 200
    assert rule_hits_response.status_code == 200
    assert reports_response.status_code == 200
    assert clauses_response.json()["items"][0]["clause_id"]
    assert any(item["fact_key"] == "payment.prepay_ratio" for item in facts_response.json()["items"])
    assert any(item["rule_id"] == "FIN-PUR-003" for item in rule_hits_response.json()["items"])

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

    actions_response = client.get(f"/api/tasks/{task['id']}/review-actions")
    updated_hits_response = client.get(f"/api/tasks/{task['id']}/rule-hits")
    updated_hits = updated_hits_response.json()["items"]

    assert actions_response.json()["items"][0]["action_type"] == "request_evidence"
    assert next(item for item in updated_hits if item["rule_id"] == "FIN-PUR-003")["review_status"] == "evidence_requested"

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
