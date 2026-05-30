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
    monkeypatch.setenv("RAGFLOW_BASE_URL", "http://127.0.0.1:65530")
    monkeypatch.setenv("LLM_API_KEY", "test-secret-value")
    monkeypatch.setenv("LLM_PROBE_ENABLED", "0")

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


def test_create_task_and_fetch_review_payload(client: TestClient) -> None:
    sample_path = sample_contract("采购合同-样本B-收款账户不一致风险版.md")
    payload = sample_path.read_bytes()

    create_response = client.post(
        "/api/tasks",
        data={"contract_name": "办公电脑采购合同"},
        files={"file": ("contract.md", payload, "text/markdown")},
    )

    assert create_response.status_code == 201
    task = create_response.json()["task"]
    assert task["overall_risk"] == "red"

    detail_response = client.get(f"/api/tasks/{task['id']}")
    assert detail_response.status_code == 200
    review_task = detail_response.json()["task"]
    rule_ids = {item["rule"] for item in review_task["risks"]}

    assert "FIN-PUR-003" in rule_ids
    assert "FIN-PUR-005" in rule_ids
    assert review_task["task"]["risk"] == "高风险"


def test_rejects_unsupported_upload_type(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        data={"contract_name": "bad file"},
        files={"file": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 400
    assert ".md" in response.json()["detail"]


def test_dashboard_and_review_pages_render_created_task(client: TestClient) -> None:
    sample_path = sample_contract("服务合同-样本B-高预付款风险版.md")
    payload = sample_path.read_bytes()

    response = client.post(
        "/tasks/create",
        data={"contract_name": "营销系统开发服务合同"},
        files={"file": ("service.md", payload, "text/markdown")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    review_url = response.headers["location"]

    dashboard_response = client.get("/")
    assert dashboard_response.status_code == 200
    assert "营销系统开发服务合同" in dashboard_response.text

    review_api_response = client.get("/api/tasks")
    task_id = review_api_response.json()["items"][0]["id"]
    detail_response = client.get(f"/api/tasks/{task_id}")
    rule_ids = {item["rule"] for item in detail_response.json()["task"]["risks"]}

    assert "FIN-SVC-004" in rule_ids
    assert "FIN-SVC-006" in rule_ids

    review_response = client.get(review_url)
    assert review_response.status_code == 200
    assert "RAGFlow 状态" in review_response.text
    assert "模型服务" in review_response.text
    assert "高风险" in review_response.text


def test_system_status_does_not_leak_llm_secret(client: TestClient) -> None:
    response = client.get("/api/system/status")

    assert response.status_code == 200
    payload_text = response.text
    payload = response.json()

    assert "test-secret-value" not in payload_text
    assert payload["llm"]["api_key_present"] is True
    assert payload["llm"]["status"] == "configured"
