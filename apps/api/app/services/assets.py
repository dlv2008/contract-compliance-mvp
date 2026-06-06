from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import (
    AssetAuditEvent,
    AssetEditLock,
    AssetSourceChunk,
    AssetSourceDocument,
    ConfigAsset,
    LLMExecutionRecord,
    ReviewProfile,
    ReviewProfileAssetRef,
)
from app.services.llm import LLMClient


BASIC_PROFILE_ID = "profile-basic-contract-review-v1"
PROCUREMENT_BASIC_PROFILE_ID = "profile-procurement-basic-v1"
PROCUREMENT_ADVANCED_PROFILE_ID = "profile-procurement-advanced-v1"
SERVICE_BASIC_PROFILE_ID = "profile-service-basic-v1"

ASSET_STATUS_FLOW = {"draft", "approved", "active", "retired", "rejected"}
PROFILE_STATUS_FLOW = {"draft", "active", "retired"}
SINGLETON_ASSET_TYPES = {
    "clause_parse_template",
    "extraction_schema",
    "risk_evaluation_policy",
    "risk_message_template",
    "report_template",
}

ASSET_EXECUTION_STATUS = {
    "policy_reference": {
        "status": "implemented",
        "label": "已接入执行",
        "tone": "ok",
        "summary": "配置集绑定的 policy_reference 会进入任务冻结快照，用于风险依据和报告引用渲染；静态标题仅作为兜底。",
        "next_step": "后续把 seed policy_reference 迁移到数据库初始化脚本，并接入 RAGFlow 原文依据。",
    },
    "clause_parse_template": {
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但条款解析仍使用 review_engine.py 中的固定正则和段落降级逻辑。",
        "next_step": "Step 6：条款解析模板资产执行。",
    },
    "extraction_schema": {
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但字段抽取仍由代码内置关键字和正则完成。",
        "next_step": "Step 7：字段 Schema 和静态提取规则资产执行。",
    },
    "extraction_rule": {
        "status": "planned",
        "label": "未接入执行",
        "tone": "muted",
        "summary": "资产类型已预留，当前没有执行器读取 extraction_rule。",
        "next_step": "Step 7：字段 Schema 和静态提取规则资产执行。",
    },
    "hard_rule": {
        "status": "implemented",
        "label": "已接入执行",
        "tone": "ok",
        "summary": "配置集绑定的 active hard_rule 会进入 rule_context，并由 review_engine.evaluate_hard_rule() 执行。",
        "next_step": "Step 9：hard_rule DSL 升级。",
    },
    "semantic_rule": {
        "status": "implemented",
        "label": "已接入执行",
        "tone": "ok",
        "summary": "配置集绑定的 semantic_rule 会进入 rule_context，并由审查引擎通过 LLM/mock runner 输出结构化判断；低置信或无证据不会直接生成风险。",
        "next_step": "后续补充语义规则可视化审核、LLM execution 落库和可恢复 workflow。",
    },
    "risk_evaluation_policy": {
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但 overall risk、status 和 decision 仍由 review_engine.py 中的代码派生。",
        "next_step": "Step 12：risk policy、message template、report template 资产执行。",
    },
    "risk_message_template": {
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但风险提示目前来自 hard_rule 模板和代码渲染，通用 message template 尚未接管。",
        "next_step": "Step 12：risk policy、message template、report template 资产执行。",
    },
    "report_template": {
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但报告章节和 Markdown 输出仍由代码模板生成。",
        "next_step": "Step 12：risk policy、message template、report template 资产执行。",
    },
    "prompt_template": {
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但 LLM 草稿生成和语义判断尚未读取 prompt_template 资产。",
        "next_step": "Step 4：LLM 草稿生成 v1，替换 mock generate_rule_drafts。",
    },
    "seed_profile": {
        "status": "planned",
        "label": "非执行资产",
        "tone": "muted",
        "summary": "seed_profile 仅用于资产类型兼容展示，不直接参与审查执行。",
        "next_step": "后续可移除或改为初始化脚本概念。",
    },
}


class AssetNotFoundError(ValueError):
    pass


class AssetStateError(ValueError):
    pass


class AssetStore(Protocol):
    def load_state(self) -> tuple[list[ConfigAsset], list[ReviewProfile]]:
        pass

    def save_state(self, assets: list[ConfigAsset], profiles: list[ReviewProfile]) -> None:
        pass


class JsonAssetStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_state(self) -> tuple[list[ConfigAsset], list[ReviewProfile]]:
        if not self.state_path.exists():
            self.save_state([], [])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            assets = [ConfigAsset.model_validate(item) for item in payload.get("assets", [])]
            profiles = [ReviewProfile.model_validate(item) for item in payload.get("profiles", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AssetStateError("配置资产仓库无法读取。") from exc
        return assets, profiles

    def save_state(self, assets: list[ConfigAsset], profiles: list[ReviewProfile]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {
            "assets": [asset.model_dump() for asset in assets],
            "profiles": [profile.model_dump() for profile in profiles],
        }
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


class AssetSourceDocumentStore(Protocol):
    def load_documents(self) -> list[AssetSourceDocument]:
        pass

    def save_documents(self, documents: list[AssetSourceDocument]) -> None:
        pass


class JsonAssetSourceDocumentStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_documents(self) -> list[AssetSourceDocument]:
        if not self.state_path.exists():
            self.save_documents([])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [AssetSourceDocument.model_validate(item) for item in payload.get("documents", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AssetStateError("制度来源文档仓库无法读取。") from exc

    def save_documents(self, documents: list[AssetSourceDocument]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {"documents": [document.model_dump() for document in documents]}
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


class LLMExecutionStore(Protocol):
    def load_executions(self) -> list[LLMExecutionRecord]:
        pass

    def save_executions(self, executions: list[LLMExecutionRecord]) -> None:
        pass


class JsonLLMExecutionStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_executions(self) -> list[LLMExecutionRecord]:
        if not self.state_path.exists():
            self.save_executions([])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [LLMExecutionRecord.model_validate(item) for item in payload.get("executions", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AssetStateError("LLM 执行记录仓库无法读取。") from exc

    def save_executions(self, executions: list[LLMExecutionRecord]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {"executions": [execution.model_dump() for execution in executions]}
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


class AssetAuditStore(Protocol):
    def load_events(self) -> list[AssetAuditEvent]:
        pass

    def save_events(self, events: list[AssetAuditEvent]) -> None:
        pass


class JsonAssetAuditStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_events(self) -> list[AssetAuditEvent]:
        if not self.state_path.exists():
            self.save_events([])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [AssetAuditEvent.model_validate(item) for item in payload.get("events", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AssetStateError("资产审计事件仓库无法读取。") from exc

    def save_events(self, events: list[AssetAuditEvent]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {"events": [event.model_dump() for event in events]}
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


class AssetEditLockStore(Protocol):
    def load_locks(self) -> list[AssetEditLock]:
        pass

    def save_locks(self, locks: list[AssetEditLock]) -> None:
        pass


class JsonAssetEditLockStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_locks(self) -> list[AssetEditLock]:
        if not self.state_path.exists():
            self.save_locks([])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [AssetEditLock.model_validate(item) for item in payload.get("locks", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AssetStateError("资产编辑锁仓库无法读取。") from exc

    def save_locks(self, locks: list[AssetEditLock]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {"locks": [lock.model_dump() for lock in locks]}
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_asset_content_hash(
    *,
    asset_type: str,
    applicability: dict,
    content: dict,
    schema_version: str,
) -> str:
    payload = {
        "asset_type": asset_type,
        "applicability": applicability,
        "content": content,
        "schema_version": schema_version,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_source_text_hash(source_text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in source_text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _asset(
    asset_id: str,
    asset_type: str,
    name: str,
    *,
    applicability: dict | None = None,
    content: dict | None = None,
    schema_version: str = "asset-v1",
    description: str | None = None,
) -> ConfigAsset:
    now = utc_now()
    resolved_applicability = applicability or {}
    resolved_content = content or {}
    return ConfigAsset(
        id=asset_id,
        asset_type=asset_type,
        name=name,
        version=1,
        status="active",
        applicability=resolved_applicability,
        content=resolved_content,
        schema_version=schema_version,
        content_hash=compute_asset_content_hash(
            asset_type=asset_type,
            applicability=resolved_applicability,
            content=resolved_content,
            schema_version=schema_version,
        ),
        description=description,
        created_by="seed",
        approved_by="seed",
        effective_from=now,
        created_at=now,
        updated_at=now,
    )


def _hard_rule(
    asset_id: str,
    name: str,
    *,
    contract_type: str,
    rule_id: str,
    title: str,
    level: str,
    conditions: list[dict],
    evidence_fact_keys: list[str],
    policy_reference_ids: list[str],
    reason_template: str,
    action_template: str,
    description: str | None = None,
) -> ConfigAsset:
    return _asset(
        asset_id,
        "hard_rule",
        name,
        applicability={"contract_type": contract_type},
        content={
            "rule_id": rule_id,
            "title": title,
            "level": level,
            "conditions": conditions,
            "evidence_fact_keys": evidence_fact_keys,
            "policy_reference_ids": policy_reference_ids,
            "reason_template": reason_template,
            "action_template": action_template,
        },
        schema_version="hard-rule-v2",
        description=description,
    )


SEED_ASSETS: list[ConfigAsset] = [
    _asset(
        "asset-clause-standard-cn-v1",
        "clause_parse_template",
        "标准中文条款编号解析",
        content={
            "header_pattern": r"^【(?P<id>[A-Z]\d{3})】(?P<title>.+?)\s*$",
            "fallback": "paragraph_split",
        },
        schema_version="clause-parse-template-v1",
        description="识别【A001】标题格式，无法识别时按段落降级。",
    ),
    _asset(
        "asset-extraction-procurement-v1",
        "extraction_schema",
        "采购合同字段 Schema",
        applicability={"contract_type": "procurement_contract"},
        content={
            "fields": [
                "contract_type",
                "party_a_name",
                "party_b_name",
                "amount_total",
                "invoice.type",
                "invoice.tax_rate",
                "payment.prepay_ratio",
                "payment.final_condition",
                "acceptance.required",
                "warranty.period_months",
                "account.same_as_counterparty",
            ]
        },
        schema_version="extraction-schema-v1",
        description="采购合同审查需要抽取的基础字段集合。",
    ),
    _asset(
        "asset-extraction-service-v1",
        "extraction_schema",
        "服务合同字段 Schema",
        applicability={"contract_type": "service_contract"},
        content={
            "fields": [
                "contract_type",
                "party_a_name",
                "party_b_name",
                "amount_total",
                "payment.prepay_ratio",
                "acceptance.required",
                "term.auto_renewal",
                "dispute.location",
                "liability.reciprocal",
            ]
        },
        schema_version="extraction-schema-v1",
        description="服务合同审查需要抽取的基础字段集合。",
    ),
    _hard_rule(
        "asset-hardrule-procurement-invoice-required-v1",
        "采购合同发票条款必填",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-001",
        title="缺失发票条款",
        level="medium",
        conditions=[{"fact_key": "invoice.type", "operator": "missing"}],
        evidence_fact_keys=[],
        policy_reference_ids=["POLICY-PUR-001", "POLICY-FUND-002"],
        reason_template="合同文本中未发现明确发票类型、开票方式或开票要求，后续付款审核口径不完整。",
        action_template="补充发票类型、开票时点和发票内容，确保合同与付款资料一致。",
        description="采购合同必须明确发票类型、开票方式或开票要求。",
    ),
    _hard_rule(
        "asset-hardrule-service-invoice-required-v1",
        "服务合同发票条款必填",
        contract_type="service_contract",
        rule_id="FIN-SVC-001",
        title="缺失发票条款",
        level="medium",
        conditions=[{"fact_key": "invoice.type", "operator": "missing"}],
        evidence_fact_keys=[],
        policy_reference_ids=["POLICY-FUND-002"],
        reason_template="合同文本中未发现明确发票类型、开票方式或开票要求，后续付款审核口径不完整。",
        action_template="补充发票类型、开票时点和发票内容，确保合同与付款资料一致。",
        description="服务合同必须明确发票类型、开票方式或开票要求。",
    ),
    _hard_rule(
        "asset-hardrule-procurement-tax-rate-required-v1",
        "采购合同税率条款必填",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-002",
        title="缺失税率条款",
        level="medium",
        conditions=[
            {"fact_key": "invoice.type", "operator": "present"},
            {"fact_key": "invoice.tax_rate", "operator": "missing"},
        ],
        evidence_fact_keys=["invoice.type"],
        policy_reference_ids=["POLICY-FUND-002"],
        reason_template="合同提到了发票，但未写明税率或税点，税务处理和付款审核依据不足。",
        action_template="补充明确税率、发票类型及开票内容，避免后续开票与付款口径不一致。",
        description="出现发票要求时必须明确税率。",
    ),
    _hard_rule(
        "asset-hardrule-service-tax-rate-required-v1",
        "服务合同税率条款必填",
        contract_type="service_contract",
        rule_id="FIN-SVC-002",
        title="缺失税率条款",
        level="medium",
        conditions=[
            {"fact_key": "invoice.type", "operator": "present"},
            {"fact_key": "invoice.tax_rate", "operator": "missing"},
        ],
        evidence_fact_keys=["invoice.type"],
        policy_reference_ids=["POLICY-FUND-002"],
        reason_template="合同提到了发票，但未写明税率或税点，税务处理和付款审核依据不足。",
        action_template="补充明确税率、发票类型及开票内容，避免后续开票与付款口径不一致。",
        description="出现发票要求时必须明确税率。",
    ),
    _hard_rule(
        "asset-hardrule-prepay-v1",
        "预付款比例控制",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-003",
        title="采购预付款比例超过 {threshold}%",
        level="high",
        conditions=[{"fact_key": "payment.prepay_ratio", "operator": ">", "value": 30}],
        evidence_fact_keys=["payment.prepay_ratio"],
        policy_reference_ids=["POLICY-PUR-002", "POLICY-FUND-006"],
        reason_template="合同约定预付款比例为 {payment.prepay_ratio}，已超过配置资产 {asset_id} 设定的 {threshold}% 阈值。",
        action_template="将预付款比例降至 {threshold}% 以内，或补充采购与财务的例外审批说明。",
        description="当预付款比例超过阈值时命中风险。",
    ),
    _hard_rule(
        "asset-hardrule-service-prepay-v1",
        "服务预付款比例控制",
        contract_type="service_contract",
        rule_id="FIN-SVC-003",
        title="服务预付款比例超过 {threshold}%",
        level="medium",
        conditions=[
            {"fact_key": "payment.prepay_ratio", "operator": ">", "value": 30},
            {"fact_key": "acceptance.required", "operator": "is", "value": False},
        ],
        evidence_fact_keys=["payment.prepay_ratio"],
        policy_reference_ids=["POLICY-FUND-003"],
        reason_template="服务合同预付款比例为 {payment.prepay_ratio}，已超过配置资产 {asset_id} 设定的 {threshold}% 阈值。",
        action_template="建议补充例外审批依据，或将预付款比例调整至 {threshold}% 以内。",
        description="服务合同预付款超过阈值但未与验收节点绑定时命中中风险。",
    ),
    _hard_rule(
        "asset-hardrule-service-prepay-acceptance-v1",
        "服务高预付款且尾款依赖验收",
        contract_type="service_contract",
        rule_id="FIN-SVC-004",
        title="高预付款且尾款依赖验收",
        level="high",
        conditions=[
            {"fact_key": "payment.prepay_ratio", "operator": ">", "value": 30},
            {"fact_key": "acceptance.required", "operator": "is", "value": True},
        ],
        evidence_fact_keys=["payment.prepay_ratio", "acceptance.required"],
        policy_reference_ids=["POLICY-FUND-003", "POLICY-FUND-004", "POLICY-FUND-006"],
        reason_template="服务合同预付款比例为 {payment.prepay_ratio}，超过配置资产 {asset_id} 的 {threshold}% 阈值，且尾款与交付/验收节点绑定，资金前置风险较高。",
        action_template="压低预付款比例至 {threshold}% 以内，并增加阶段性交付与验收控制，必要时进入联合复核。",
        description="服务合同高预付款且尾款依赖验收时命中高风险。",
    ),
    _hard_rule(
        "asset-hardrule-procurement-acceptance-required-v1",
        "采购合同验收条款必填",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-004",
        title="缺失验收条款",
        level="medium",
        conditions=[{"fact_key": "acceptance.required", "operator": "is", "value": False}],
        evidence_fact_keys=[],
        policy_reference_ids=["POLICY-PUR-003"],
        reason_template="合同中未发现明确验收、交付成果确认或到货验收安排，后续尾款支付依据不足。",
        action_template="补充明确的验收标准、验收主体和验收节点，避免以默认认可替代关键确认。",
        description="采购合同应明确验收标准和节点。",
    ),
    _hard_rule(
        "asset-hardrule-service-acceptance-required-v1",
        "服务合同验收条款必填",
        contract_type="service_contract",
        rule_id="FIN-SVC-005",
        title="缺失验收条款",
        level="medium",
        conditions=[{"fact_key": "acceptance.required", "operator": "is", "value": False}],
        evidence_fact_keys=[],
        policy_reference_ids=["POLICY-FUND-004"],
        reason_template="合同中未发现明确验收、交付成果确认或到货验收安排，后续尾款支付依据不足。",
        action_template="补充明确的验收标准、验收主体和验收节点，避免以默认认可替代关键确认。",
        description="服务合同应明确验收标准和节点。",
    ),
    _hard_rule(
        "asset-hardrule-payee-v1",
        "收款主体一致性控制",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-005",
        title="收款账户主体与签约乙方不一致",
        level="high",
        conditions=[{"fact_key": "account.same_as_counterparty", "operator": "is", "value": False}],
        evidence_fact_keys=["account.same_as_counterparty"],
        policy_reference_ids=["POLICY-FUND-005", "POLICY-PUR-005"],
        reason_template="合同乙方与收款账户名称不一致，存在代收款、关联方收款或账户变更未留痕的风险。",
        action_template="要求提供专项审批和账户变更证明，或改回与签约主体一致的收款账户。",
        description="收款账户主体与签约相对方不一致时命中风险。",
    ),
    _hard_rule(
        "asset-hardrule-service-payee-v1",
        "服务收款主体一致性控制",
        contract_type="service_contract",
        rule_id="FIN-SVC-009",
        title="收款账户主体与签约乙方不一致",
        level="high",
        conditions=[{"fact_key": "account.same_as_counterparty", "operator": "is", "value": False}],
        evidence_fact_keys=["account.same_as_counterparty"],
        policy_reference_ids=["POLICY-FUND-005"],
        reason_template="合同乙方与收款账户名称不一致，存在代收款、关联方收款或账户变更未留痕的风险。",
        action_template="要求提供专项审批和账户变更证明，或改回与签约主体一致的收款账户。",
        description="服务合同收款账户主体与签约相对方不一致时命中风险。",
    ),
    _hard_rule(
        "asset-hardrule-procurement-auto-renewal-v1",
        "采购自动续约审批前提控制",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-007",
        title="自动续约缺少审批前提",
        level="high",
        conditions=[
            {"fact_key": "term.auto_renewal", "operator": "is", "value": True},
            {"fact_key": "approval.exception_required", "operator": "is", "value": False},
        ],
        evidence_fact_keys=["term.auto_renewal"],
        policy_reference_ids=["POLICY-REV-004", "POLICY-PUR-006"],
        reason_template="合同包含自动续约或默认顺延安排，但未看到重新审批、书面续签或授权边界控制。",
        action_template="改为到期后重新审批并签署书面续约文件，不建议使用默认自动顺延。",
        description="采购合同自动续约必须有审批或书面续签前提。",
    ),
    _hard_rule(
        "asset-hardrule-service-auto-renewal-v1",
        "服务自动续约审批前提控制",
        contract_type="service_contract",
        rule_id="FIN-SVC-006",
        title="自动续约缺少审批前提",
        level="high",
        conditions=[
            {"fact_key": "term.auto_renewal", "operator": "is", "value": True},
            {"fact_key": "approval.exception_required", "operator": "is", "value": False},
        ],
        evidence_fact_keys=["term.auto_renewal"],
        policy_reference_ids=["POLICY-REV-004"],
        reason_template="合同包含自动续约或默认顺延安排，但未看到重新审批、书面续签或授权边界控制。",
        action_template="改为到期后重新审批并签署书面续约文件，不建议使用默认自动顺延。",
        description="服务合同自动续约必须有审批或书面续签前提。",
    ),
    _hard_rule(
        "asset-hardrule-service-dispute-location-v1",
        "服务争议解决地偏向乙方控制",
        contract_type="service_contract",
        rule_id="FIN-SVC-007",
        title="争议解决地约定偏向乙方",
        level="medium",
        conditions=[{"fact_key": "dispute.raw", "operator": "contains", "value": "乙方所在地"}],
        evidence_fact_keys=["dispute.location"],
        policy_reference_ids=["POLICY-REV-005"],
        reason_template="争议解决条款将仲裁或诉讼安排在乙方所在地，后续维权和举证成本偏高。",
        action_template="建议改为甲方所在地法院或双方均可接受的争议解决地。",
        description="服务合同争议解决地不宜单方面偏向乙方。",
    ),
    _hard_rule(
        "asset-hardrule-service-liability-reciprocal-v1",
        "服务违约责任对等控制",
        contract_type="service_contract",
        rule_id="FIN-SVC-008",
        title="违约责任明显不对等",
        level="high",
        conditions=[{"fact_key": "liability.reciprocal", "operator": "is", "value": False}],
        evidence_fact_keys=["liability.reciprocal"],
        policy_reference_ids=["POLICY-REV-001"],
        reason_template="合同对乙方责任设置了明显上限，但对甲方迟延付款责任扩张至全部损失和预期收益，责任分配失衡。",
        action_template="将双方违约责任调整为对等口径，并统一责任边界或赔偿上限。",
        description="服务合同违约责任应保持对等。",
    ),
    _hard_rule(
        "asset-hardrule-procurement-warranty-required-v1",
        "采购质保或保修条款必填",
        contract_type="procurement_contract",
        rule_id="FIN-PUR-006",
        title="缺失质保或保修条款",
        level="medium",
        conditions=[{"fact_key": "warranty.present", "operator": "is", "value": False}],
        evidence_fact_keys=[],
        policy_reference_ids=["POLICY-PUR-004"],
        reason_template="采购合同中未看到明确质保期限、保修责任或售后承诺，后续设备问题缺少追责抓手。",
        action_template="补充质保期限、保修范围、响应时限与更换维修责任。",
        description="采购合同应明确质保或保修安排。",
    ),
    _asset(
        "asset-semantic-auto-renewal-v1",
        "semantic_rule",
        "自动续约缺少审批前提",
        applicability={"contract_type": ["procurement_contract", "service_contract"]},
        content={
            "prompt_template_id": "asset-prompt-semantic-rule-v1",
            "output_schema": "semantic-rule-result-v1",
        },
        schema_version="semantic-rule-v1",
        description="判断自动续约是否缺少审批、书面续签或退出机制。",
    ),
    _asset(
        "asset-risk-policy-basic-v1",
        "risk_evaluation_policy",
        "基础风险评估策略",
        content={
            "high": {"overall_risk": "red", "status": "pending_review"},
            "medium": {"overall_risk": "yellow", "status": "watchlist"},
            "none": {"overall_risk": "green", "status": "ready"},
        },
        schema_version="risk-policy-v1",
        description="把规则风险等级映射为红黄绿、任务状态和待办类型。",
    ),
    _asset(
        "asset-risk-message-basic-v1",
        "risk_message_template",
        "基础风险提示模板",
        content={"template": "{rule_title}: {reason} 建议：{action}"},
        schema_version="risk-message-template-v1",
        description="风险卡片和整改建议的基础文案模板。",
    ),
    _asset(
        "asset-report-compliance-basic-v1",
        "report_template",
        "合规审查交付报告",
        content={"sections": ["current_status", "summary", "recommendation", "rule_hits", "review_actions"]},
        schema_version="report-template-v1",
        description="当前交付报告使用的基础章节模板。",
    ),
    _asset(
        "asset-prompt-rule-draft-v1",
        "prompt_template",
        "制度文档生成规则草稿 Prompt",
        content={"purpose": "rule_draft", "output_schema": "rule-draft-v1"},
        schema_version="prompt-template-v1",
        description="指导 LLM/草稿生成器从制度文档中提取规则草稿。",
    ),
    _asset(
        "asset-prompt-field-extraction-v1",
        "prompt_template",
        "低置信字段抽取候选 Prompt",
        content={
            "purpose": "field_extraction",
            "output_schema": "field-extraction-candidates-v1",
            "instructions": "仅为静态规则未提取到的字段生成候选值、证据条款和置信度，不直接改变规则裁决。",
        },
        schema_version="prompt-template-v1",
        description="指导 LLM 对 missing 字段生成候选事实，供合同审核员确认。",
    ),
    _asset(
        "asset-prompt-semantic-rule-v1",
        "prompt_template",
        "语义规则结构化判断 Prompt",
        content={"purpose": "semantic_rule", "output_schema": "semantic-rule-result-v1"},
        schema_version="prompt-template-v1",
        description="指导语义规则输出结构化判断结果。",
    ),
]


def _ref(asset_id: str, reason: str | None = None) -> ReviewProfileAssetRef:
    asset = next(item for item in SEED_ASSETS if item.id == asset_id)
    return ReviewProfileAssetRef(
        asset_id=asset.id,
        asset_type=asset.asset_type,
        asset_version=asset.version,
        binding_reason=reason,
    )


def _profile(
    profile_id: str,
    name: str,
    *,
    applicability: dict,
    description: str,
    assets: list[ReviewProfileAssetRef],
) -> ReviewProfile:
    now = utc_now()
    return ReviewProfile(
        id=profile_id,
        name=name,
        version=1,
        status="active",
        applicability=applicability,
        description=description,
        assets=assets,
        created_by="seed",
        published_by="seed",
        created_at=now,
        updated_at=now,
    )


SEED_PROFILES: list[ReviewProfile] = [
    _profile(
        BASIC_PROFILE_ID,
        "基础通用合同审查",
        applicability={"contract_type": "unknown_contract"},
        description="用于旧任务迁移和最小演示的通用配置集。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
    _profile(
        PROCUREMENT_BASIC_PROFILE_ID,
        "采购合同基础审查",
        applicability={"contract_type": "procurement_contract"},
        description="采购合同基础配置集，覆盖条款解析、采购字段、核心硬规则和基础报告模板。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-extraction-procurement-v1"),
            _ref("asset-hardrule-procurement-invoice-required-v1"),
            _ref("asset-hardrule-procurement-tax-rate-required-v1"),
            _ref("asset-hardrule-prepay-v1", "采购预付款比例控制。"),
            _ref("asset-hardrule-payee-v1", "收款主体一致性控制。"),
            _ref("asset-hardrule-procurement-acceptance-required-v1"),
            _ref("asset-hardrule-procurement-auto-renewal-v1"),
            _ref("asset-hardrule-procurement-warranty-required-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
    _profile(
        PROCUREMENT_ADVANCED_PROFILE_ID,
        "采购合同升级审查",
        applicability={"contract_type": "procurement_contract", "profile_level": "advanced"},
        description="采购合同升级配置集，包含语义规则示例和 Prompt 模板。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-extraction-procurement-v1"),
            _ref("asset-hardrule-procurement-invoice-required-v1"),
            _ref("asset-hardrule-procurement-tax-rate-required-v1"),
            _ref("asset-hardrule-prepay-v1"),
            _ref("asset-hardrule-payee-v1"),
            _ref("asset-hardrule-procurement-acceptance-required-v1"),
            _ref("asset-hardrule-procurement-auto-renewal-v1"),
            _ref("asset-hardrule-procurement-warranty-required-v1"),
            _ref("asset-semantic-auto-renewal-v1"),
            _ref("asset-prompt-semantic-rule-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
    _profile(
        SERVICE_BASIC_PROFILE_ID,
        "服务合同基础审查",
        applicability={"contract_type": "service_contract"},
        description="服务合同基础配置集，覆盖服务合同字段、预付款、续约和责任条款关注点。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-extraction-service-v1"),
            _ref("asset-hardrule-service-invoice-required-v1"),
            _ref("asset-hardrule-service-tax-rate-required-v1"),
            _ref("asset-hardrule-service-prepay-v1"),
            _ref("asset-hardrule-service-prepay-acceptance-v1"),
            _ref("asset-hardrule-service-acceptance-required-v1"),
            _ref("asset-hardrule-service-payee-v1"),
            _ref("asset-hardrule-service-auto-renewal-v1"),
            _ref("asset-hardrule-service-dispute-location-v1"),
            _ref("asset-hardrule-service-liability-reciprocal-v1"),
            _ref("asset-semantic-auto-renewal-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
]


class AssetRegistry:
    def __init__(
        self,
        settings: Settings | None = None,
        store: AssetStore | None = None,
        source_store: AssetSourceDocumentStore | None = None,
        llm_execution_store: LLMExecutionStore | None = None,
        audit_store: AssetAuditStore | None = None,
        edit_lock_store: AssetEditLockStore | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if (
            store is None
            or source_store is None
            or llm_execution_store is None
            or audit_store is None
            or edit_lock_store is None
        ):
            default_store, default_source_store, default_llm_store, default_audit_store, default_lock_store = self._default_stores()
        else:
            default_store, default_source_store, default_llm_store, default_audit_store, default_lock_store = (
                store,
                source_store,
                llm_execution_store,
                audit_store,
                edit_lock_store,
            )
        self.store = store or default_store
        self.source_store = source_store or default_source_store
        self.llm_execution_store = llm_execution_store or default_llm_store
        self.audit_store = audit_store or default_audit_store
        self.edit_lock_store = edit_lock_store or default_lock_store
        self.llm_client = llm_client or LLMClient(self.settings)

    def _default_stores(
        self,
    ) -> tuple[AssetStore, AssetSourceDocumentStore, LLMExecutionStore, AssetAuditStore, AssetEditLockStore]:
        if self.settings.asset_store_backend == "postgres":
            from app.services.db_store import (
                PostgresAssetAuditStore,
                PostgresAssetEditLockStore,
                PostgresAssetSourceDocumentStore,
                PostgresAssetStore,
                PostgresLLMExecutionStore,
            )

            return (
                PostgresAssetStore(self.settings),
                PostgresAssetSourceDocumentStore(self.settings),
                PostgresLLMExecutionStore(self.settings),
                PostgresAssetAuditStore(self.settings),
                PostgresAssetEditLockStore(self.settings),
            )
        return (
            JsonAssetStore(self.settings.data_dir / "assets.json"),
            JsonAssetSourceDocumentStore(self.settings.data_dir / "asset_source_documents.json"),
            JsonLLMExecutionStore(self.settings.data_dir / "llm_executions.json"),
            JsonAssetAuditStore(self.settings.data_dir / "asset_audit_events.json"),
            JsonAssetEditLockStore(self.settings.data_dir / "asset_edit_locks.json"),
        )

    def list_source_documents(self, *, q: str | None = None) -> list[AssetSourceDocument]:
        documents = self.source_store.load_documents()
        if q:
            needle = q.strip().lower()
            documents = [
                item
                for item in documents
                if needle in item.name.lower() or needle in item.id.lower() or needle in item.content_hash.lower()
            ]
        return sorted(documents, key=lambda item: item.created_at or "", reverse=True)

    def get_source_document(self, document_id: str) -> AssetSourceDocument:
        document = next((item for item in self.source_store.load_documents() if item.id == document_id), None)
        if document is None:
            raise AssetNotFoundError("制度来源文档不存在。")
        return document

    def delete_source_document(self, document_id: str) -> None:
        documents = self.source_store.load_documents()
        document = next((item for item in documents if item.id == document_id), None)
        if document is None:
            raise AssetNotFoundError("制度来源文档不存在。")
        assets, _ = self._load_state()
        referenced_assets = [
            asset
            for asset in assets
            if asset.content.get("source_document_id") == document_id
            and asset.status not in {"rejected"}
        ]
        if referenced_assets:
            raise AssetStateError("该来源文档已被资产草稿引用，不能直接删除。请先处理或删除相关草稿。")
        self.source_store.save_documents([item for item in documents if item.id != document_id])

    def create_source_document(
        self,
        *,
        name: str,
        source_text: str,
        source_type: str = "policy_document",
        metadata: dict | None = None,
        actor: str = "reviewer",
    ) -> AssetSourceDocument:
        resolved_text = source_text.strip()
        if not resolved_text:
            raise AssetStateError("制度来源文档内容不能为空。")
        now = utc_now()
        document_id = f"source-doc-{uuid.uuid4().hex[:10]}"
        chunks = self._split_source_document(document_id, resolved_text)
        document = AssetSourceDocument(
            id=document_id,
            source_type=source_type or "policy_document",
            name=name.strip() or "未命名制度文档",
            content_text=resolved_text,
            content_hash=compute_source_text_hash(resolved_text),
            chunks=chunks,
            metadata={
                **(metadata or {}),
                "chunk_count": len(chunks),
                "char_count": len(resolved_text),
                "splitter": "static-policy-splitter-v1",
            },
            created_by=actor or "reviewer",
            created_at=now,
            updated_at=now,
        )
        documents = self.source_store.load_documents()
        documents.append(document)
        self.source_store.save_documents(documents)
        return document

    def list_llm_executions(self, *, purpose: str | None = None) -> list[LLMExecutionRecord]:
        executions = self.llm_execution_store.load_executions()
        if purpose:
            executions = [item for item in executions if item.purpose == purpose]
        return sorted(executions, key=lambda item: item.created_at, reverse=True)

    def delete_asset(self, asset_id: str, *, actor: str = "reviewer") -> None:
        assets, profiles = self._load_state()
        asset = next((item for item in assets if item.id == asset_id), None)
        if asset is None:
            raise AssetNotFoundError("配置资产不存在。")
        self._ensure_asset_write_allowed(asset_id, actor=actor)
        if asset.status not in {"draft", "rejected"}:
            raise AssetStateError("只能删除 draft 或 rejected 状态的资产。approved/active 资产请走驳回、发布或版本化流程。")
        self._save_state([item for item in assets if item.id != asset_id], profiles)
        self._record_audit_event(
            target_type="asset",
            target_id=asset.id,
            action="asset.delete",
            actor=actor or "reviewer",
            message=f"Deleted asset draft {asset.name}.",
            before_hash=asset.content_hash,
            metadata={"asset_type": asset.asset_type, "status": asset.status},
        )

    def update_asset_draft(
        self,
        asset_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        applicability: dict | None = None,
        content: dict | None = None,
        schema_version: str | None = None,
        expected_content_hash: str | None = None,
        actor: str = "reviewer",
    ) -> ConfigAsset:
        assets, profiles = self._load_state()
        asset = next((item for item in assets if item.id == asset_id), None)
        if asset is None:
            raise AssetNotFoundError("配置资产不存在。")
        self._ensure_asset_write_allowed(asset_id, actor=actor)
        if asset.status != "draft":
            raise AssetStateError("只能编辑 draft 状态的资产。")

        if expected_content_hash and asset.content_hash and expected_content_hash != asset.content_hash:
            raise AssetStateError("资产草稿已被其他操作更新，请刷新页面后再编辑。")

        resolved_applicability = applicability if applicability is not None else dict(asset.applicability)
        resolved_content = content if content is not None else dict(asset.content)
        resolved_schema_version = schema_version or asset.schema_version
        candidate = asset.model_copy(
            update={
                "name": name or asset.name,
                "description": description if description is not None else asset.description,
                "applicability": resolved_applicability,
                "content": resolved_content,
                "schema_version": resolved_schema_version,
                "content_hash": compute_asset_content_hash(
                    asset_type=asset.asset_type,
                    applicability=resolved_applicability,
                    content=resolved_content,
                    schema_version=resolved_schema_version,
                ),
                "updated_at": utc_now(),
            }
        )
        self.validate_asset_draft(candidate)
        self._save_state([candidate if item.id == asset_id else item for item in assets], profiles)
        self._record_audit_event(
            target_type="asset",
            target_id=asset_id,
            action="asset.update_draft",
            actor=actor or "reviewer",
            message=f"Updated asset draft {candidate.name}.",
            before_hash=asset.content_hash,
            after_hash=candidate.content_hash,
            metadata={"asset_type": candidate.asset_type, "schema_version": candidate.schema_version},
        )
        return candidate

    def list_audit_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> list[AssetAuditEvent]:
        events = self.audit_store.load_events()
        if target_type:
            events = [item for item in events if item.target_type == target_type]
        if target_id:
            events = [item for item in events if item.target_id == target_id]
        if action:
            events = [item for item in events if item.action == action]
        return sorted(events, key=lambda item: item.created_at, reverse=True)[:limit]

    def list_edit_locks(self, *, asset_id: str | None = None) -> list[AssetEditLock]:
        locks = self._active_edit_locks()
        if asset_id:
            locks = [item for item in locks if item.asset_id == asset_id]
        return sorted(locks, key=lambda item: item.acquired_at, reverse=True)

    def acquire_edit_lock(
        self,
        asset_id: str,
        *,
        actor: str = "reviewer",
        purpose: str = "edit",
        ttl_minutes: int = 30,
    ) -> AssetEditLock:
        self.get_asset(asset_id)
        actor = actor or "reviewer"
        active_locks = self._active_edit_locks()
        conflicting = next((item for item in active_locks if item.asset_id == asset_id and item.actor != actor), None)
        if conflicting:
            raise AssetStateError(f"资产已由 {conflicting.actor} 锁定编辑，请等待释放或过期后再操作。")
        locks = [item for item in active_locks if item.asset_id != asset_id or item.actor == actor]
        existing = next((item for item in locks if item.asset_id == asset_id and item.actor == actor), None)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=max(1, min(ttl_minutes, 240)))
        lock = AssetEditLock(
            id=existing.id if existing else f"asset-lock-{uuid.uuid4().hex[:10]}",
            asset_id=asset_id,
            actor=actor,
            purpose=purpose or "edit",
            acquired_at=existing.acquired_at if existing else now.isoformat(timespec="seconds"),
            expires_at=expires_at.isoformat(timespec="seconds"),
        )
        locks = [lock if item.id == lock.id else item for item in locks]
        if existing is None:
            locks.append(lock)
        self.edit_lock_store.save_locks(locks)
        self._record_audit_event(
            target_type="asset",
            target_id=asset_id,
            action="asset.lock_acquire",
            actor=actor,
            message=f"Acquired edit lock for asset {asset_id}.",
            metadata={"lock_id": lock.id, "expires_at": lock.expires_at, "purpose": lock.purpose},
        )
        return lock

    def release_edit_lock(self, asset_id: str, *, actor: str = "reviewer") -> None:
        before = self._active_edit_locks()
        after = [item for item in before if not (item.asset_id == asset_id and item.actor == (actor or "reviewer"))]
        self.edit_lock_store.save_locks(after)
        if len(after) != len(before):
            self._record_audit_event(
                target_type="asset",
                target_id=asset_id,
                action="asset.lock_release",
                actor=actor or "reviewer",
                message=f"Released edit lock for asset {asset_id}.",
            )

    def _active_edit_locks(self) -> list[AssetEditLock]:
        now = datetime.now(timezone.utc)
        active = []
        for lock in self.edit_lock_store.load_locks():
            try:
                expires_at = datetime.fromisoformat(lock.expires_at)
            except ValueError:
                continue
            if expires_at > now:
                active.append(lock)
        if len(active) != len(self.edit_lock_store.load_locks()):
            self.edit_lock_store.save_locks(active)
        return active

    def _ensure_asset_write_allowed(self, asset_id: str, *, actor: str = "reviewer") -> None:
        resolved_actor = actor or "reviewer"
        conflicting = next(
            (item for item in self._active_edit_locks() if item.asset_id == asset_id and item.actor != resolved_actor),
            None,
        )
        if conflicting:
            raise AssetStateError(
                f"资产已由 {conflicting.actor} 锁定编辑，当前操作不能覆盖他人正在处理的版本。"
            )

    def validate_asset_draft(self, asset: ConfigAsset) -> list[str]:
        errors: list[str] = []
        if not asset.name.strip():
            errors.append("资产名称不能为空。")
        if not isinstance(asset.applicability, dict):
            errors.append("适用性必须是 JSON 对象。")
        if not isinstance(asset.content, dict):
            errors.append("内容必须是 JSON 对象。")
        if errors:
            raise AssetStateError("；".join(errors))

        if asset.asset_type == "hard_rule":
            self._validate_hard_rule_draft_content(asset.content)
        elif asset.asset_type == "semantic_rule":
            self._validate_semantic_rule_content(asset.content)
        elif asset.asset_type == "extraction_rule":
            self._validate_extraction_rule_content(asset.content)
        elif asset.asset_type == "policy_reference":
            self._validate_policy_reference_content(asset.content)
        return errors

    def list_assets(
        self,
        *,
        asset_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
    ) -> list[ConfigAsset]:
        assets, _ = self._load_state()
        items = assets
        if asset_type:
            items = [item for item in items if item.asset_type == asset_type]
        if status:
            items = [item for item in items if item.status == status]
        if q:
            needle = q.strip().lower()
            items = [item for item in items if needle in item.name.lower() or needle in item.id.lower()]
        return sorted(items, key=lambda item: (item.asset_type, item.status, item.name, -item.version))

    def list_profiles(
        self,
        *,
        status: str | None = "active",
        contract_type: str | None = None,
    ) -> list[ReviewProfile]:
        _, profiles = self._load_state()
        items = profiles
        if status:
            items = [item for item in items if item.status == status]
        if contract_type:
            items = [
                item
                for item in items
                if item.applicability.get("contract_type") in {contract_type, "unknown_contract"}
            ]
        return sorted(items, key=lambda item: (item.status != "active", item.name, -item.version))

    def get_asset(self, asset_id: str) -> ConfigAsset:
        assets, _ = self._load_state()
        asset = next((item for item in assets if item.id == asset_id), None)
        if asset is None:
            raise AssetNotFoundError("配置资产不存在。")
        return asset

    def get_profile(self, profile_id: str) -> ReviewProfile:
        _, profiles = self._load_state()
        profile = next((item for item in profiles if item.id == profile_id), None)
        if profile is None:
            raise AssetNotFoundError("审查配置集不存在。")
        return profile

    def get_active_profile(self, profile_id: str | None) -> ReviewProfile:
        if not profile_id:
            raise AssetNotFoundError("请选择审查配置集。")
        profile = self.get_profile(profile_id)
        if profile.status != "active":
            raise AssetNotFoundError("审查配置集不存在或未生效。")
        return profile

    def create_asset_draft(
        self,
        *,
        asset_type: str,
        name: str,
        applicability: dict | None = None,
        content: dict | None = None,
        schema_version: str | None = None,
        description: str | None = None,
        parent_asset_id: str | None = None,
        actor: str = "reviewer",
    ) -> ConfigAsset:
        if asset_type not in self.asset_types():
            raise AssetStateError("不支持的配置资产类型。")
        assets, profiles = self._load_state()
        resolved_applicability = applicability or {}
        resolved_content = content or {}
        resolved_schema_version = schema_version or f"{asset_type.replace('_', '-')}-v1"
        version = self._next_asset_version(assets, name, resolved_applicability)
        now = utc_now()
        draft = ConfigAsset(
            id=f"asset-{asset_type.replace('_', '-')}-{uuid.uuid4().hex[:8]}",
            asset_type=asset_type,
            name=name,
            version=version,
            status="draft",
            applicability=resolved_applicability,
            content=resolved_content,
            schema_version=resolved_schema_version,
            content_hash=compute_asset_content_hash(
                asset_type=asset_type,
                applicability=resolved_applicability,
                content=resolved_content,
                schema_version=resolved_schema_version,
            ),
            description=description,
            parent_asset_id=parent_asset_id,
            created_by=actor or "reviewer",
            created_at=now,
            updated_at=now,
        )
        assets.append(draft)
        self._save_state(assets, profiles)
        self._record_audit_event(
            target_type="asset",
            target_id=draft.id,
            action="asset.create_draft",
            actor=actor or "reviewer",
            message=f"Created asset draft {draft.name}.",
            after_hash=draft.content_hash,
            metadata={"asset_type": draft.asset_type, "version": draft.version},
        )
        return draft

    def generate_rule_drafts(
        self,
        *,
        source_text: str | None = None,
        source_document_id: str | None = None,
        source_type: str = "policy_document",
        draft_types: list[str] | None = None,
        profile_hint: dict | None = None,
        actor: str = "reviewer",
    ) -> dict:
        document: AssetSourceDocument | None = None
        resolved_source_text = (source_text or "").strip()
        if source_document_id:
            document = self.get_source_document(source_document_id)
            resolved_source_text = resolved_source_text or document.content_text
            source_type = document.source_type
        if not resolved_source_text:
            raise AssetStateError("制度文档内容不能为空。")
        selected_types = draft_types or ["policy_reference", "hard_rule", "semantic_rule", "extraction_rule"]
        contract_type = (profile_hint or {}).get("contract_type") or "procurement_contract"
        chunks = (
            document.chunks
            if document
            else self._split_source_document(f"inline-source-{uuid.uuid4().hex[:8]}", resolved_source_text)
        )
        prompt_template = self._active_prompt_template("rule_draft")
        execution_id = f"llm-draft-{uuid.uuid4().hex[:10]}"
        input_payload = {
            "source_document_id": document.id if document else None,
            "source_content_hash": document.content_hash if document else compute_source_text_hash(resolved_source_text),
            "source_type": source_type,
            "contract_type": contract_type,
            "draft_types": selected_types,
            "chunks": [chunk.model_dump() for chunk in chunks],
            "prompt_template_id": prompt_template.id if prompt_template else None,
        }
        provider = self.settings.llm_draft_provider
        raw_output_preview = None
        latency_ms = None
        error_detail = None
        status = "success"
        model = self.settings.llm_model

        try:
            if provider == "invalid_mock":
                llm_payload = self._invalid_llm_rule_draft_payload()
                raw_output_preview = json.dumps(llm_payload, ensure_ascii=False)[:500]
                model = "invalid-mock-draft-generator"
            elif provider == "mock" or (provider == "auto" and not self.settings.llm_api_key):
                llm_payload = self._mock_llm_rule_draft_payload(
                    source_text=resolved_source_text,
                    chunks=chunks,
                    selected_types=selected_types,
                    contract_type=contract_type,
                    source_type=source_type,
                )
                raw_output_preview = json.dumps(llm_payload, ensure_ascii=False)[:500]
                model = "mock-draft-generator"
                status = "mock_success"
            else:
                llm_result = self.llm_client.complete_json(
                    messages=self._build_rule_draft_messages(
                        prompt_template=prompt_template,
                        source_text=resolved_source_text,
                        chunks=chunks,
                        selected_types=selected_types,
                        contract_type=contract_type,
                    )
                )
                llm_payload = llm_result["parsed_json"]
                raw_output_preview = llm_result["raw_text"][:500]
                latency_ms = llm_result["latency_ms"]
                model = llm_result["model"]
        except Exception as exc:  # noqa: BLE001
            if provider != "auto":
                self._record_llm_execution(
                    execution_id=execution_id,
                    prompt_template_id=prompt_template.id if prompt_template else None,
                    model=model,
                    input_payload=input_payload,
                    output_payload={},
                    raw_output_preview=raw_output_preview,
                    status="error",
                    latency_ms=latency_ms,
                    error_detail=str(exc),
                )
                raise AssetStateError("LLM 草稿生成失败，请检查模型配置或稍后重试。") from exc
            error_detail = str(exc)
            llm_payload = self._mock_llm_rule_draft_payload(
                source_text=resolved_source_text,
                chunks=chunks,
                selected_types=selected_types,
                contract_type=contract_type,
                source_type=source_type,
            )
            raw_output_preview = json.dumps(llm_payload, ensure_ascii=False)[:500]
            model = "mock-draft-generator"
            status = "mock_fallback"

        try:
            draft_specs = self._validate_rule_draft_payload(llm_payload, selected_types=selected_types)
        except AssetStateError as exc:
            self._record_llm_execution(
                execution_id=execution_id,
                prompt_template_id=prompt_template.id if prompt_template else None,
                model=model,
                input_payload=input_payload,
                output_payload=llm_payload if isinstance(llm_payload, dict) else {},
                raw_output_preview=raw_output_preview,
                status="validation_error",
                latency_ms=latency_ms,
                error_detail=str(exc),
            )
            raise AssetStateError("LLM 输出结构化校验失败，未生成草稿资产。") from exc

        drafts = self._create_drafts_from_specs(
            draft_specs,
            actor=actor,
            contract_type=contract_type,
            execution_id=execution_id,
            prompt_template_id=prompt_template.id if prompt_template else None,
            source_document=document,
            source_text=resolved_source_text,
            source_type=source_type,
        )
        execution = self._record_llm_execution(
            execution_id=execution_id,
            prompt_template_id=prompt_template.id if prompt_template else None,
            model=model,
            input_payload=input_payload,
            output_payload=llm_payload,
            raw_output_preview=raw_output_preview,
            status=status,
            latency_ms=latency_ms,
            error_detail=error_detail,
        )
        return {"drafts": drafts, "llm_execution": execution.model_dump()}

    def _active_prompt_template(self, purpose: str) -> ConfigAsset | None:
        assets, _ = self._load_state()
        return next(
            (
                asset
                for asset in assets
                if asset.asset_type == "prompt_template"
                and asset.status == "active"
                and asset.content.get("purpose") == purpose
            ),
            None,
        )

    def _build_rule_draft_messages(
        self,
        *,
        prompt_template: ConfigAsset | None,
        source_text: str,
        chunks: list[AssetSourceChunk],
        selected_types: list[str],
        contract_type: str,
    ) -> list[dict[str, str]]:
        chunk_text = "\n\n".join(
            f"[{chunk.id}] {chunk.title}\n{chunk.text}" for chunk in chunks[:12]
        )
        template_hint = json.dumps(prompt_template.content if prompt_template else {}, ensure_ascii=False)
        return [
            {
                "role": "system",
                "content": (
                    "你是企业合同合规审查配置资产生成助手。"
                    "请只输出合法 JSON 对象，不要输出 Markdown。"
                    "输出格式必须为 {\"drafts\": [...]}。"
                    "drafts 中每个对象包含 asset_type, name, applicability, schema_version, content, description。"
                    "首批允许的 asset_type 为 policy_reference, hard_rule, semantic_rule, extraction_rule。"
                    "hard_rule.content 必须包含 rule_id, title, level, conditions, evidence_fact_keys, "
                    "policy_reference_ids, reason_template, action_template。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Prompt 模板资产内容：{template_hint}\n"
                    f"适用合同类型：{contract_type}\n"
                    f"需要生成的资产类型：{', '.join(selected_types)}\n"
                    f"来源 chunks：\n{chunk_text}\n\n"
                    f"完整来源文本：\n{source_text[:6000]}"
                ),
            },
        ]

    def _mock_llm_rule_draft_payload(
        self,
        *,
        source_text: str,
        chunks: list[AssetSourceChunk],
        selected_types: list[str],
        contract_type: str,
        source_type: str,
    ) -> dict:
        threshold = self._extract_percent_threshold(source_text) or 30
        first_chunk_ids = [chunk.id for chunk in chunks[:2]]
        policy_reference_id = "POLICY-DRAFT-001"
        rule_id = "FIN-PUR-003" if contract_type == "procurement_contract" else "FIN-SVC-003"
        rule_title = (
            f"采购预付款比例超过 {threshold}%"
            if contract_type == "procurement_contract"
            else f"服务预付款比例超过 {threshold}%"
        )
        drafts = []
        if "policy_reference" in selected_types:
            drafts.append(
                {
                    "asset_type": "policy_reference",
                    "name": f"制度依据草稿：预付款 {threshold}% 控制",
                    "applicability": {"contract_type": contract_type},
                    "schema_version": "policy-reference-v1",
                    "description": "由来源文档生成的制度依据草稿，用于规则溯源。",
                    "content": {
                        "reference_id": policy_reference_id,
                        "title": f"预付款比例不得超过 {threshold}%",
                        "summary": source_text.strip()[:240],
                        "source_chunk_ids": first_chunk_ids,
                        "source_type": source_type,
                    },
                }
            )
        if "hard_rule" in selected_types:
            drafts.append(
                {
                    "asset_type": "hard_rule",
                    "name": f"制度草稿：预付款比例超过 {threshold}% 控制",
                    "applicability": {"contract_type": contract_type},
                    "schema_version": "hard-rule-v2",
                    "description": "由 LLM 草稿生成链路生成的硬规则草稿，审核发布并绑定配置集后才会生效。",
                    "content": {
                        "rule_id": rule_id,
                        "title": rule_title,
                        "level": "high" if contract_type == "procurement_contract" else "medium",
                        "conditions": [
                            {"fact_key": "payment.prepay_ratio", "operator": ">", "value": threshold}
                        ],
                        "fact_key": "payment.prepay_ratio",
                        "operator": ">",
                        "value": threshold,
                        "risk_level": "high" if contract_type == "procurement_contract" else "medium",
                        "evidence_fact_keys": ["payment.prepay_ratio"],
                        "policy_reference_ids": [policy_reference_id],
                        "reason_template": (
                            "合同约定预付款比例为 {payment.prepay_ratio}，"
                            f"已超过制度草稿设定的 {threshold}% 阈值。"
                        ),
                        "action_template": (
                            f"将预付款比例降至 {threshold}% 以内，或补充例外审批说明。"
                        ),
                        "source_chunk_ids": first_chunk_ids,
                    },
                }
            )
        if "semantic_rule" in selected_types:
            drafts.append(
                {
                    "asset_type": "semantic_rule",
                    "name": "制度草稿：例外审批材料语义检查",
                    "applicability": {"contract_type": contract_type},
                    "schema_version": "semantic-rule-v1",
                    "description": "由 LLM 草稿生成链路生成的语义规则草稿。",
                    "content": {
                        "prompt_template_id": "asset-prompt-semantic-rule-v1",
                        "question": "当合同预付款超过制度阈值时，是否补充了采购和财务例外审批材料？",
                        "output_schema": "semantic-rule-result-v1",
                        "policy_reference_ids": [policy_reference_id],
                        "source_chunk_ids": first_chunk_ids,
                    },
                }
            )
        if "extraction_rule" in selected_types:
            drafts.append(
                {
                    "asset_type": "extraction_rule",
                    "name": "制度草稿：预付款比例提取规则",
                    "applicability": {"contract_type": contract_type},
                    "schema_version": "extraction-rule-v1",
                    "description": "由 LLM 草稿生成链路生成的字段提取规则草稿。",
                    "content": {
                        "fact_key": "payment.prepay_ratio",
                        "label": "预付款比例",
                        "patterns": ["预付款", "支付合同总价"],
                        "value_type": "percent",
                        "source_chunk_ids": first_chunk_ids,
                    },
                }
            )
        return {"drafts": drafts}

    def _invalid_llm_rule_draft_payload(self) -> dict:
        return {
            "drafts": [
                {
                    "asset_type": "hard_rule",
                    "name": "",
                    "content": {"conditions": "not-a-list"},
                }
            ]
        }

    def _validate_rule_draft_payload(self, payload: dict, *, selected_types: list[str]) -> list[dict]:
        if not isinstance(payload, dict):
            raise AssetStateError("LLM 输出顶层必须是对象。")
        drafts = payload.get("drafts")
        if not isinstance(drafts, list):
            raise AssetStateError("LLM 输出缺少 drafts 数组。")
        allowed_types = {"policy_reference", "hard_rule", "semantic_rule", "extraction_rule"}
        selected = set(selected_types)
        valid_drafts = []
        for draft in drafts:
            if not isinstance(draft, dict):
                raise AssetStateError("drafts 中存在非对象元素。")
            draft = self._normalize_llm_draft_spec(draft)
            asset_type = draft.get("asset_type")
            if asset_type not in allowed_types:
                raise AssetStateError(f"不支持的草稿资产类型：{asset_type}。")
            if asset_type not in selected:
                continue
            name = draft.get("name")
            content = draft.get("content")
            if not isinstance(name, str) or not name.strip():
                raise AssetStateError("草稿资产缺少 name。")
            if not isinstance(content, dict):
                raise AssetStateError("草稿资产缺少 content 对象。")
            if asset_type == "hard_rule":
                self._validate_hard_rule_draft_content(content)
            valid_drafts.append(draft)
        if not valid_drafts:
            raise AssetStateError("LLM 输出没有可生成的草稿资产。")
        return valid_drafts

    def _normalize_llm_draft_spec(self, draft: dict) -> dict:
        normalized = dict(draft)
        applicability = normalized.get("applicability")
        if isinstance(applicability, str):
            normalized["applicability"] = {"contract_type": applicability}
        elif not isinstance(applicability, dict):
            normalized["applicability"] = {}

        content = normalized.get("content")
        if not isinstance(content, dict):
            return normalized
        normalized_content = dict(content)

        if "source_chunks" in normalized_content and "source_chunk_ids" not in normalized_content:
            normalized_content["source_chunk_ids"] = normalized_content["source_chunks"]
        if "evidence_fact_key" in normalized_content and "evidence_fact_keys" not in normalized_content:
            normalized_content["evidence_fact_keys"] = normalized_content["evidence_fact_key"]
        if isinstance(normalized_content.get("evidence_fact_keys"), str):
            normalized_content["evidence_fact_keys"] = [normalized_content["evidence_fact_keys"]]
        if isinstance(normalized_content.get("policy_reference_ids"), str):
            normalized_content["policy_reference_ids"] = [normalized_content["policy_reference_ids"]]

        if normalized.get("asset_type") == "hard_rule":
            normalized_content = self._normalize_hard_rule_content(normalized_content)

        normalized["content"] = normalized_content
        return normalized

    def _normalize_hard_rule_content(self, content: dict) -> dict:
        normalized = dict(content)
        level = str(normalized.get("level") or normalized.get("risk_level") or "medium").lower()
        if level in {"mandatory", "required", "critical", "严重", "强制"}:
            normalized["level"] = "high"

        raw_condition_tree = normalized.get("condition_tree") or normalized.get("condition") or normalized.get("where")
        if isinstance(raw_condition_tree, dict) and raw_condition_tree:
            normalized["condition_tree"] = self._normalize_condition_tree(raw_condition_tree)

        raw_conditions = normalized.get("conditions")
        if isinstance(raw_conditions, dict):
            if any(key in raw_conditions for key in ["all", "any", "not"]):
                normalized["condition_tree"] = self._normalize_condition_tree(raw_conditions)
                normalized["conditions"] = []
                raw_conditions = {}
            threshold_items = raw_conditions.get("threshold")
            if isinstance(threshold_items, list):
                normalized["conditions"] = [
                    self._normalize_condition_item(item)
                    for item in threshold_items
                    if isinstance(item, dict)
                ]
            else:
                trigger_condition = raw_conditions.get("trigger_condition")
                if trigger_condition:
                    normalized["semantic_condition"] = trigger_condition
                normalized["conditions"] = []

        if not normalized.get("conditions") and normalized.get("fact_key"):
            normalized["conditions"] = [
                {
                    "fact_key": normalized.get("fact_key"),
                    "operator": normalized.get("operator") or "is",
                    "value": normalized.get("value"),
                }
            ]

        if not normalized.get("evidence_fact_keys"):
            fact_keys = self._condition_tree_fact_keys(normalized.get("condition_tree"))
            if not fact_keys:
                fact_keys = [
                    str(condition.get("fact_key"))
                    for condition in normalized.get("conditions", [])
                    if isinstance(condition, dict) and condition.get("fact_key")
                ]
            normalized["evidence_fact_keys"] = fact_keys

        return normalized

    def _normalize_condition_tree(self, node: dict) -> dict:
        normalized = dict(node)
        for branch_key in ["all", "any"]:
            children = normalized.get(branch_key)
            if isinstance(children, list):
                normalized[branch_key] = [
                    self._normalize_condition_tree(child)
                    for child in children
                    if isinstance(child, dict)
                ]
        not_child = normalized.get("not")
        if isinstance(not_child, dict):
            normalized["not"] = self._normalize_condition_tree(not_child)
        if not any(key in normalized for key in ["all", "any", "not"]) and normalized.get("fact_key"):
            normalized = self._normalize_condition_item(normalized)
        return normalized

    def _normalize_condition_item(self, item: dict) -> dict:
        fact_key = item.get("fact_key") or item.get("target_field") or item.get("field") or "payment.amount"
        normalized = {
            "fact_key": str(fact_key),
            "operator": item.get("operator") or ">",
            "value": item.get("value"),
        }
        if item.get("expected_fact_key"):
            normalized["expected_fact_key"] = str(item["expected_fact_key"])
        if item.get("range") is not None:
            normalized["range"] = item["range"]
        return normalized

    def _condition_tree_fact_keys(self, node: object) -> list[str]:
        if isinstance(node, list):
            keys: list[str] = []
            for child in node:
                keys.extend(self._condition_tree_fact_keys(child))
            return list(dict.fromkeys(keys))
        if not isinstance(node, dict):
            return []
        for branch_key in ["all", "any"]:
            if branch_key in node:
                return self._condition_tree_fact_keys(node.get(branch_key))
        if "not" in node:
            return self._condition_tree_fact_keys(node.get("not"))
        keys = []
        if node.get("fact_key"):
            keys.append(str(node["fact_key"]))
        if node.get("expected_fact_key"):
            keys.append(str(node["expected_fact_key"]))
        return keys

    def _validate_hard_rule_draft_content(self, content: dict) -> None:
        required_keys = {
            "rule_id",
            "title",
            "level",
            "evidence_fact_keys",
            "policy_reference_ids",
            "reason_template",
            "action_template",
        }
        missing = sorted(key for key in required_keys if key not in content)
        if missing:
            raise AssetStateError(f"hard_rule 草稿缺少字段：{', '.join(missing)}。")
        has_conditions = isinstance(content.get("conditions"), list) and bool(content.get("conditions"))
        has_tree = isinstance(content.get("condition_tree"), dict) and bool(content.get("condition_tree"))
        has_legacy_condition = bool(content.get("fact_key"))
        if not any([has_conditions, has_tree, has_legacy_condition]):
            raise AssetStateError("hard_rule requires conditions, condition_tree, or fact_key.")
        if has_conditions:
            for condition in content["conditions"]:
                self._validate_hard_rule_condition(condition)
        if has_tree:
            self._validate_hard_rule_condition_tree(content["condition_tree"])
        return

    def _validate_hard_rule_condition_tree(self, node: object) -> None:
        if not isinstance(node, dict) or not node:
            raise AssetStateError("hard_rule.condition_tree must be a non-empty object.")
        for branch_key in ["all", "any"]:
            if branch_key in node:
                children = node.get(branch_key)
                if not isinstance(children, list) or not children:
                    raise AssetStateError(f"hard_rule.condition_tree.{branch_key} must be a non-empty array.")
                for child in children:
                    self._validate_hard_rule_condition_tree(child)
                return
        if "not" in node:
            self._validate_hard_rule_condition_tree(node["not"])
            return
        self._validate_hard_rule_condition(node)

    def _validate_hard_rule_condition(self, condition: object) -> None:
        if not isinstance(condition, dict):
            raise AssetStateError("hard_rule condition must be an object.")
        for key in ["fact_key", "operator"]:
            if key not in condition:
                raise AssetStateError(f"hard_rule condition missing {key}.")
        operator = str(condition.get("operator") or "").lower()
        if "value" not in condition and "expected_fact_key" not in condition and operator not in {"missing", "present"}:
            raise AssetStateError("hard_rule condition requires value or expected_fact_key.")

    def _validate_semantic_rule_content(self, content: dict) -> None:
        if not any(content.get(key) for key in ["question", "semantic_definitions", "validation_rules"]):
            raise AssetStateError("semantic_rule 草稿至少需要 question、semantic_definitions 或 validation_rules。")

    def _validate_extraction_rule_content(self, content: dict) -> None:
        if not any(content.get(key) for key in ["fact_key", "patterns", "extraction_targets"]):
            raise AssetStateError("extraction_rule 草稿至少需要 fact_key/patterns 或 extraction_targets。")

    def _validate_policy_reference_content(self, content: dict) -> None:
        if not any(content.get(key) for key in ["reference_id", "title", "summary", "reference_items"]):
            raise AssetStateError("policy_reference 草稿至少需要 reference_id、title、summary 或 reference_items。")

    def _create_drafts_from_specs(
        self,
        draft_specs: list[dict],
        *,
        actor: str,
        contract_type: str,
        execution_id: str,
        prompt_template_id: str | None,
        source_document: AssetSourceDocument | None,
        source_text: str,
        source_type: str,
    ) -> list[ConfigAsset]:
        drafts = []
        source_content_hash = source_document.content_hash if source_document else compute_source_text_hash(source_text)
        for spec in draft_specs:
            content = {
                **spec["content"],
                "source_type": source_type,
                "source_document_id": source_document.id if source_document else None,
                "source_content_hash": source_content_hash,
                "llm_execution_id": execution_id,
                "prompt_template_id": prompt_template_id,
            }
            drafts.append(
                self.create_asset_draft(
                    asset_type=spec["asset_type"],
                    name=spec["name"],
                    applicability=spec.get("applicability") or {"contract_type": contract_type},
                    content=content,
                    schema_version=spec.get("schema_version"),
                    description=spec.get("description"),
                    actor=actor,
                )
            )
        return drafts

    def _record_llm_execution(
        self,
        *,
        execution_id: str,
        prompt_template_id: str | None,
        model: str,
        input_payload: dict,
        output_payload: dict,
        raw_output_preview: str | None,
        status: str,
        latency_ms: float | None,
        error_detail: str | None,
    ) -> LLMExecutionRecord:
        execution = LLMExecutionRecord(
            id=execution_id,
            purpose="rule_draft",
            prompt_template_id=prompt_template_id,
            model=model,
            input_payload=input_payload,
            output_payload=output_payload,
            raw_output_preview=raw_output_preview,
            status=status,
            latency_ms=latency_ms,
            created_at=utc_now(),
            error_detail=error_detail,
        )
        executions = self.llm_execution_store.load_executions()
        executions.append(execution)
        self.llm_execution_store.save_executions(executions)
        return execution

    def approve_asset(self, asset_id: str, *, actor: str = "reviewer", comment: str | None = None) -> ConfigAsset:
        self._ensure_asset_write_allowed(asset_id, actor=actor)
        self.validate_asset_draft(self.get_asset(asset_id))
        return self._transition_asset(
            asset_id,
            expected={"draft"},
            status="approved",
            actor=actor,
            approval_comment=comment,
        )

    def reject_asset(self, asset_id: str, *, actor: str = "reviewer", comment: str | None = None) -> ConfigAsset:
        self._ensure_asset_write_allowed(asset_id, actor=actor)
        return self._transition_asset(
            asset_id,
            expected={"draft", "approved"},
            status="rejected",
            actor=actor,
            rejection_comment=comment,
        )

    def publish_asset(self, asset_id: str, *, actor: str = "reviewer") -> ConfigAsset:
        self._ensure_asset_write_allowed(asset_id, actor=actor)
        return self._transition_asset(
            asset_id,
            expected={"approved"},
            status="active",
            actor=actor,
            effective_from=utc_now(),
        )

    def clone_asset(
        self,
        asset_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        actor: str = "reviewer",
    ) -> ConfigAsset:
        assets, profiles = self._load_state()
        source = next((item for item in assets if item.id == asset_id), None)
        if source is None:
            raise AssetNotFoundError("配置资产不存在。")
        self._ensure_asset_write_allowed(asset_id, actor=actor)
        version = max(source.version + 1, self._next_asset_version(assets, source.name, source.applicability))
        now = utc_now()
        draft_id = self._next_asset_id(source, version, assets)
        draft = ConfigAsset(
            id=draft_id,
            asset_type=source.asset_type,
            name=name or source.name,
            version=version,
            status="draft",
            applicability=dict(source.applicability),
            content=dict(source.content),
            schema_version=source.schema_version,
            content_hash=source.content_hash,
            description=description if description is not None else source.description,
            parent_asset_id=source.id,
            created_by=actor or "reviewer",
            created_at=now,
            updated_at=now,
        )
        assets.append(draft)
        self._save_state(assets, profiles)
        self._record_audit_event(
            target_type="asset",
            target_id=draft.id,
            action="asset.clone",
            actor=actor or "reviewer",
            message=f"Cloned asset {source.id} to draft {draft.id}.",
            before_hash=source.content_hash,
            after_hash=draft.content_hash,
            metadata={"source_asset_id": source.id, "version": draft.version},
        )
        return draft

    def clone_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        actor: str = "reviewer",
    ) -> ReviewProfile:
        assets, profiles = self._load_state()
        source = next((item for item in profiles if item.id == profile_id), None)
        if source is None:
            raise AssetNotFoundError("审查配置集不存在。")
        base_name = name or f"{source.name} 新版本"
        version = self._next_profile_version(profiles, source)
        now = utc_now()
        clone = ReviewProfile(
            id=f"{source.id.rsplit('-v', 1)[0]}-v{version}" if "-v" in source.id else f"profile-{uuid.uuid4().hex[:8]}",
            name=base_name,
            version=version,
            status="draft",
            applicability=dict(source.applicability),
            description=description or source.description,
            assets=[item.model_copy() for item in source.assets],
            parent_profile_id=source.id,
            created_by=actor or "reviewer",
            created_at=now,
            updated_at=now,
        )
        if any(item.id == clone.id for item in profiles):
            clone = clone.model_copy(update={"id": f"profile-{uuid.uuid4().hex[:10]}"})
        profiles.append(clone)
        self._save_state(assets, profiles)
        self._record_audit_event(
            target_type="profile",
            target_id=clone.id,
            action="profile.clone",
            actor=actor or "reviewer",
            message=f"Cloned profile {source.id} to draft {clone.id}.",
            metadata={"source_profile_id": source.id, "version": clone.version},
        )
        return clone

    def bind_asset_to_profile(
        self,
        profile_id: str,
        asset_id: str,
        *,
        required: bool = True,
        binding_reason: str | None = None,
    ) -> ReviewProfile:
        assets, profiles = self._load_state()
        profile = next((item for item in profiles if item.id == profile_id), None)
        asset = next((item for item in assets if item.id == asset_id), None)
        if profile is None:
            raise AssetNotFoundError("审查配置集不存在。")
        if asset is None:
            raise AssetNotFoundError("配置资产不存在。")
        if profile.status != "draft":
            raise AssetStateError("只能修改草稿状态的配置集。请先复制为新版本。")
        if asset.status != "active":
            raise AssetStateError("只能绑定 active 状态的资产。")

        new_ref = ReviewProfileAssetRef(
            asset_id=asset.id,
            asset_type=asset.asset_type,
            asset_version=asset.version,
            required=required,
            binding_reason=binding_reason,
        )
        refs = list(profile.assets)
        if asset.asset_type in SINGLETON_ASSET_TYPES:
            refs = [item for item in refs if item.asset_type != asset.asset_type]
        elif asset.asset_type == "hard_rule":
            refs = [
                item
                for item in refs
                if not self._is_same_hard_rule_target(item, asset, assets)
            ]
        else:
            refs = [item for item in refs if item.asset_id != asset.id]
        refs.append(new_ref)
        updated = profile.model_copy(update={"assets": refs, "updated_at": utc_now()})
        profiles = [updated if item.id == profile_id else item for item in profiles]
        self._save_state(assets, profiles)
        self._record_audit_event(
            target_type="profile",
            target_id=profile_id,
            action="profile.bind_asset",
            actor="reviewer",
            message=f"Bound asset {asset.id} to profile {profile.id}.",
            after_hash=asset.content_hash,
            metadata={"asset_id": asset.id, "asset_type": asset.asset_type, "asset_version": asset.version},
        )
        return updated

    def publish_profile(
        self,
        profile_id: str,
        *,
        actor: str = "reviewer",
        comment: str | None = None,
    ) -> ReviewProfile:
        assets, profiles = self._load_state()
        profile = next((item for item in profiles if item.id == profile_id), None)
        if profile is None:
            raise AssetNotFoundError("审查配置集不存在。")
        if profile.status != "draft":
            raise AssetStateError("只有草稿配置集可以发布。")
        self._validate_profile_assets(profile, assets)
        updated = profile.model_copy(
            update={
                "status": "active",
                "published_by": actor or "reviewer",
                "publish_comment": comment,
                "updated_at": utc_now(),
            }
        )
        profiles = [updated if item.id == profile_id else item for item in profiles]
        self._save_state(assets, profiles)
        self._record_audit_event(
            target_type="profile",
            target_id=profile_id,
            action="profile.publish",
            actor=actor or "reviewer",
            message=f"Published profile {profile.name}.",
            metadata={"version": profile.version, "asset_count": len(profile.assets)},
        )
        return updated

    def freeze_profile(self, profile: ReviewProfile) -> dict:
        assets, _ = self._load_state()
        snapshot_assets = []
        for ref in profile.assets:
            asset = next((item for item in assets if item.id == ref.asset_id), None)
            if asset is None:
                continue
            snapshot_assets.append(
                {
                    "asset_id": asset.id,
                    "asset_type": asset.asset_type,
                    "name": asset.name,
                    "version": asset.version,
                    "schema_version": asset.schema_version,
                    "content_hash": asset.content_hash,
                    "status": asset.status,
                    "content": asset.content,
                }
            )
        return {
            "profile_id": profile.id,
            "profile_name": profile.name,
            "profile_version": profile.version,
            "status": profile.status,
            "applicability": profile.applicability,
            "assets": snapshot_assets,
        }

    def default_profile_for_contract_type(self, contract_type: str) -> ReviewProfile:
        if contract_type == "procurement_contract":
            return self.get_active_profile(PROCUREMENT_BASIC_PROFILE_ID)
        if contract_type == "service_contract":
            return self.get_active_profile(SERVICE_BASIC_PROFILE_ID)
        return self.get_active_profile(BASIC_PROFILE_ID)

    def rule_context_for_profile(self, profile: ReviewProfile) -> dict[str, Any]:
        assets, _ = self._load_state()
        threshold = 30
        source_asset_id = "asset-hardrule-prepay-v1"
        hard_rules = []
        clause_parse_templates = []
        extraction_schemas = []
        extraction_rules = []
        prompt_templates = []
        semantic_rules = []
        policy_references = []
        risk_evaluation_policies = []
        risk_message_templates = []
        report_templates = []
        for ref in profile.assets:
            asset = next((item for item in assets if item.id == ref.asset_id), None)
            if not asset or asset.status != "active":
                continue
            context_item = {
                "asset_id": asset.id,
                "asset_version": asset.version,
                "asset_content_hash": asset.content_hash,
                "schema_version": asset.schema_version,
                "name": asset.name,
                "description": asset.description,
                "applicability": asset.applicability,
                **asset.content,
            }
            if asset.asset_type == "clause_parse_template":
                clause_parse_templates.append(context_item)
            if asset.asset_type == "extraction_schema":
                extraction_schemas.append(context_item)
            if asset.asset_type == "extraction_rule":
                extraction_rules.append(context_item)
            if asset.asset_type == "prompt_template":
                prompt_templates.append(context_item)
            if asset.asset_type == "hard_rule":
                hard_rules.append(context_item)
                threshold_condition = self._find_threshold_condition(asset)
                if threshold_condition is not None:
                    threshold = float(threshold_condition)
                    source_asset_id = asset.id
            if asset.asset_type == "semantic_rule":
                semantic_rules.append(context_item)
            if asset.asset_type == "policy_reference":
                policy_references.append(context_item)
            if asset.asset_type == "risk_evaluation_policy":
                risk_evaluation_policies.append(context_item)
            if asset.asset_type == "risk_message_template":
                risk_message_templates.append(context_item)
            if asset.asset_type == "report_template":
                report_templates.append(context_item)
        return {
            "prepay_threshold": threshold,
            "prepay_threshold_asset_id": source_asset_id,
            "hard_rules": hard_rules,
            "clause_parse_templates": clause_parse_templates,
            "extraction_schemas": extraction_schemas,
            "extraction_rules": extraction_rules,
            "prompt_templates": prompt_templates,
            "semantic_rules": semantic_rules,
            "policy_references": policy_references,
            "risk_evaluation_policies": risk_evaluation_policies,
            "risk_message_templates": risk_message_templates,
            "report_templates": report_templates,
        }

    def execution_status_for_asset_type(self, asset_type: str) -> dict[str, str]:
        status = ASSET_EXECUTION_STATUS.get(
            asset_type,
            {
                "status": "planned",
                "label": "未接入执行",
                "tone": "muted",
                "summary": "该资产类型尚未定义执行器。",
                "next_step": "待后续设计确认。",
            },
        )
        return {"asset_type": asset_type, **status}

    def asset_execution_audit(self) -> dict[str, Any]:
        assets, profiles = self._load_state()
        profile_refs_by_type: dict[str, set[str]] = {}
        for profile in profiles:
            for ref in profile.assets:
                profile_refs_by_type.setdefault(ref.asset_type, set()).add(profile.id)

        items = []
        for asset_type in self.asset_types():
            typed_assets = [asset for asset in assets if asset.asset_type == asset_type]
            active_assets = [asset for asset in typed_assets if asset.status == "active"]
            status = self.execution_status_for_asset_type(asset_type)
            items.append(
                {
                    **status,
                    "total_assets": len(typed_assets),
                    "active_assets": len(active_assets),
                    "bound_profile_count": len(profile_refs_by_type.get(asset_type, set())),
                    "bound_profile_ids": sorted(profile_refs_by_type.get(asset_type, set())),
                }
            )

        summary: dict[str, int] = {}
        for item in items:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        return {"items": items, "summary": summary}

    def profile_execution_audit(self, profile: ReviewProfile) -> dict[str, Any]:
        assets, _ = self._load_state()
        assets_by_id = {asset.id: asset for asset in assets}
        items = []
        for ref in profile.assets:
            asset = assets_by_id.get(ref.asset_id)
            status = self.execution_status_for_asset_type(ref.asset_type)
            items.append(
                {
                    **status,
                    "asset_id": ref.asset_id,
                    "asset_name": asset.name if asset else ref.asset_id,
                    "asset_status": asset.status if asset else "missing",
                    "asset_version": ref.asset_version,
                    "required": ref.required,
                    "binding_reason": ref.binding_reason,
                }
            )

        summary: dict[str, int] = {}
        for item in items:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        return {"items": items, "summary": summary}

    def summary(self) -> dict:
        active_profiles = self.list_profiles(status="active")
        active_assets = self.list_assets(status="active")
        draft_assets = self.list_assets(status="draft")
        approved_assets = self.list_assets(status="approved")
        return {
            "active_profiles": len(active_profiles),
            "active_assets": len(active_assets),
            "draft_assets": len(draft_assets),
            "approved_assets": len(approved_assets),
            "execution_audit": self.asset_execution_audit()["summary"],
        }

    def asset_types(self) -> list[str]:
        return [
            "policy_reference",
            "clause_parse_template",
            "extraction_schema",
            "extraction_rule",
            "hard_rule",
            "semantic_rule",
            "risk_evaluation_policy",
            "risk_message_template",
            "report_template",
            "prompt_template",
            "seed_profile",
        ]

    def _split_source_document(self, document_id: str, source_text: str) -> list[AssetSourceChunk]:
        heading_pattern = re.compile(
            r"^(第[一二三四五六七八九十百千万0-9]+[章节条款]|[一二三四五六七八九十]+、|\d+(?:\.\d+)*[、.\s]|【.+?】)"
        )
        sections: list[tuple[str, int, int]] = []
        current_title = "全文"
        current_start = 0
        cursor = 0
        has_heading = False

        for raw_line in source_text.splitlines(keepends=True):
            stripped = raw_line.strip()
            if stripped and heading_pattern.match(stripped):
                if has_heading and cursor > current_start:
                    sections.append((current_title, current_start, cursor))
                current_title = stripped[:80]
                current_start = cursor
                has_heading = True
            cursor += len(raw_line)
        if has_heading:
            sections.append((current_title, current_start, len(source_text)))
        else:
            paragraph_start = 0
            for match in re.finditer(r"\n\s*\n", source_text):
                end = match.start()
                paragraph = source_text[paragraph_start:end].strip()
                if paragraph:
                    title = paragraph.splitlines()[0][:80]
                    sections.append((title, paragraph_start, end))
                paragraph_start = match.end()
            tail = source_text[paragraph_start:].strip()
            if tail:
                sections.append((tail.splitlines()[0][:80], paragraph_start, len(source_text)))

        if not sections:
            sections = [("全文", 0, len(source_text))]

        chunks: list[AssetSourceChunk] = []
        for index, (title, start, end) in enumerate(sections, start=1):
            text = source_text[start:end].strip()
            if not text:
                continue
            chunks.append(
                AssetSourceChunk(
                    id=f"{document_id}-chunk-{index:03d}",
                    document_id=document_id,
                    sequence_no=index,
                    title=title,
                    text=text,
                    char_start=start,
                    char_end=end,
                )
            )
        return chunks

    def _transition_asset(
        self,
        asset_id: str,
        *,
        expected: set[str],
        status: str,
        actor: str,
        approval_comment: str | None = None,
        rejection_comment: str | None = None,
        effective_from: str | None = None,
    ) -> ConfigAsset:
        assets, profiles = self._load_state()
        asset = next((item for item in assets if item.id == asset_id), None)
        if asset is None:
            raise AssetNotFoundError("配置资产不存在。")
        if asset.status not in expected:
            expected_text = "、".join(sorted(expected))
            raise AssetStateError(f"当前资产状态为 {asset.status}，只能从 {expected_text} 状态流转。")
        updates = {
            "status": status,
            "updated_at": utc_now(),
        }
        if status in {"approved", "active"}:
            updates["approved_by"] = actor or "reviewer"
        if approval_comment is not None:
            updates["approval_comment"] = approval_comment
        if rejection_comment is not None:
            updates["rejection_comment"] = rejection_comment
        if effective_from is not None:
            updates["effective_from"] = effective_from
        updated = asset.model_copy(update=updates)
        assets = [updated if item.id == asset_id else item for item in assets]
        self._save_state(assets, profiles)
        self._record_audit_event(
            target_type="asset",
            target_id=asset_id,
            action=f"asset.{status}",
            actor=actor or "reviewer",
            message=f"Asset {asset.name} transitioned from {asset.status} to {status}.",
            before_hash=asset.content_hash,
            after_hash=updated.content_hash,
            metadata={"from_status": asset.status, "to_status": status, "asset_type": asset.asset_type},
        )
        return updated

    def _validate_profile_assets(self, profile: ReviewProfile, assets: list[ConfigAsset]) -> None:
        active_by_id = {asset.id: asset for asset in assets if asset.status == "active"}
        for ref in profile.assets:
            asset = active_by_id.get(ref.asset_id)
            if asset is None:
                raise AssetStateError(f"配置集引用了非 active 资产：{ref.asset_id}。")
            if asset.version != ref.asset_version:
                raise AssetStateError(f"配置集引用的资产版本不一致：{ref.asset_id}。")
        required_types = {"clause_parse_template", "risk_evaluation_policy", "risk_message_template", "report_template"}
        present = {ref.asset_type for ref in profile.assets}
        missing = sorted(required_types - present)
        if missing:
            raise AssetStateError(f"配置集缺少必需资产：{', '.join(missing)}。")

    def _load_state(self) -> tuple[list[ConfigAsset], list[ReviewProfile]]:
        assets, profiles = self.store.load_state()
        assets, profiles = self._merge_seed_state(assets, profiles)
        assets, hash_changed = self._ensure_asset_content_hashes(assets)
        if hash_changed:
            self._save_state(assets, profiles)
        return assets, profiles

    def _save_state(self, assets: list[ConfigAsset], profiles: list[ReviewProfile]) -> None:
        self.store.save_state(assets, profiles)

    def _record_audit_event(
        self,
        *,
        target_type: str,
        target_id: str,
        action: str,
        actor: str,
        message: str,
        before_hash: str | None = None,
        after_hash: str | None = None,
        metadata: dict | None = None,
    ) -> AssetAuditEvent:
        event = AssetAuditEvent(
            id=f"audit-{uuid.uuid4().hex[:12]}",
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor=actor or "reviewer",
            message=message,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata=metadata or {},
            created_at=utc_now(),
        )
        events = self.audit_store.load_events()
        events.append(event)
        self.audit_store.save_events(sorted(events, key=lambda item: item.created_at, reverse=True)[:1000])
        return event

    def _ensure_asset_content_hashes(self, assets: list[ConfigAsset]) -> tuple[list[ConfigAsset], bool]:
        changed = False
        updated_assets = []
        for asset in assets:
            expected_hash = compute_asset_content_hash(
                asset_type=asset.asset_type,
                applicability=asset.applicability,
                content=asset.content,
                schema_version=asset.schema_version,
            )
            if asset.content_hash != expected_hash:
                asset = asset.model_copy(update={"content_hash": expected_hash})
                changed = True
            updated_assets.append(asset)
        return updated_assets, changed

    def _merge_seed_state(
        self,
        assets: list[ConfigAsset],
        profiles: list[ReviewProfile],
    ) -> tuple[list[ConfigAsset], list[ReviewProfile]]:
        changed = False
        assets_by_id = {asset.id: asset for asset in assets}
        for seed in SEED_ASSETS:
            existing = assets_by_id.get(seed.id)
            if existing is None:
                assets.append(seed)
                changed = True
            elif existing.created_by == "seed" and existing.model_dump() != seed.model_dump():
                assets = [seed if item.id == seed.id else item for item in assets]
                changed = True
        profiles_by_id = {profile.id: profile for profile in profiles}
        for seed in SEED_PROFILES:
            existing = profiles_by_id.get(seed.id)
            if existing is None:
                profiles.append(seed)
                changed = True
            elif existing.created_by == "seed" and existing.model_dump() != seed.model_dump():
                profiles = [seed if item.id == seed.id else item for item in profiles]
                changed = True
        if changed:
            self._save_state(assets, profiles)
        return assets, profiles

    def _next_asset_version(self, assets: list[ConfigAsset], name: str, applicability: dict) -> int:
        versions = [
            asset.version
            for asset in assets
            if asset.name == name and asset.applicability == applicability
        ]
        return max(versions, default=0) + 1

    def _next_asset_id(self, source: ConfigAsset, version: int, assets: list[ConfigAsset]) -> str:
        existing_ids = {asset.id for asset in assets}
        stem = source.id.rsplit("-v", 1)[0] if "-v" in source.id else source.id
        candidate = f"{stem}-v{version}"
        if candidate not in existing_ids:
            return candidate
        return f"asset-{source.asset_type.replace('_', '-')}-{uuid.uuid4().hex[:8]}"

    def _next_profile_version(self, profiles: list[ReviewProfile], source: ReviewProfile) -> int:
        stem = source.id.rsplit("-v", 1)[0] if "-v" in source.id else source.id
        versions = [profile.version for profile in profiles if profile.id.startswith(stem)]
        return max(versions, default=source.version) + 1

    def _extract_percent_threshold(self, source_text: str) -> int | None:
        matches = re.findall(r"(\d{1,2})\s*%", source_text)
        if not matches:
            return None
        return int(matches[0])

    def _is_same_hard_rule_target(
        self,
        ref: ReviewProfileAssetRef,
        new_asset: ConfigAsset,
        assets: list[ConfigAsset],
    ) -> bool:
        if ref.asset_type != "hard_rule":
            return False
        existing = next((asset for asset in assets if asset.id == ref.asset_id), None)
        if existing is None:
            return False
        return self._hard_rule_target_signature(existing) == self._hard_rule_target_signature(new_asset)

    def _hard_rule_target_signature(self, asset: ConfigAsset) -> tuple[str | None, str | None, str | None]:
        condition = next(
            (
                item
                for item in asset.content.get("conditions", [])
                if isinstance(item, dict) and item.get("fact_key")
            ),
            {},
        )
        fact_key = condition.get("fact_key") or asset.content.get("fact_key")
        operator = condition.get("operator") or asset.content.get("operator")
        contract_type = asset.applicability.get("contract_type")
        return (str(contract_type) if contract_type else None, str(fact_key) if fact_key else None, str(operator) if operator else None)

    def _find_threshold_condition(self, asset: ConfigAsset) -> float | None:
        for condition in asset.content.get("conditions", []):
            if condition.get("fact_key") == "payment.prepay_ratio" and condition.get("operator") == ">":
                return float(condition.get("value"))
        if asset.content.get("fact_key") == "payment.prepay_ratio" and asset.content.get("operator") == ">":
            return float(asset.content.get("value"))
        return None


def asset_counts(profile: ReviewProfile) -> dict[str, int]:
    counts: dict[str, int] = {}
    for asset in profile.assets:
        counts[asset.asset_type] = counts.get(asset.asset_type, 0) + 1
    return counts
