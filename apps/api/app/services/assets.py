from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import ConfigAsset, ReviewProfile, ReviewProfileAssetRef


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
        "status": "partially_implemented",
        "label": "部分接入",
        "tone": "muted",
        "summary": "资产可维护、可绑定并会进入配置快照；但 semantic_rule LLM Runner 尚未实现。",
        "next_step": "Step 10：semantic_rule LLM Runner。",
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    return ConfigAsset(
        id=asset_id,
        asset_type=asset_type,
        name=name,
        version=1,
        status="active",
        applicability=applicability or {},
        content=content or {},
        schema_version=schema_version,
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
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.state_path = self.settings.data_dir / "assets.json"

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
        version = self._next_asset_version(assets, name, applicability or {})
        now = utc_now()
        draft = ConfigAsset(
            id=f"asset-{asset_type.replace('_', '-')}-{uuid.uuid4().hex[:8]}",
            asset_type=asset_type,
            name=name,
            version=version,
            status="draft",
            applicability=applicability or {},
            content=content or {},
            schema_version=schema_version or f"{asset_type.replace('_', '-')}-v1",
            description=description,
            parent_asset_id=parent_asset_id,
            created_by=actor or "reviewer",
            created_at=now,
            updated_at=now,
        )
        assets.append(draft)
        self._save_state(assets, profiles)
        return draft

    def generate_rule_drafts(
        self,
        *,
        source_text: str,
        source_type: str = "policy_document",
        draft_types: list[str] | None = None,
        profile_hint: dict | None = None,
        actor: str = "reviewer",
    ) -> dict:
        if not source_text.strip():
            raise AssetStateError("制度文档内容不能为空。")
        selected_types = draft_types or ["hard_rule"]
        drafts: list[ConfigAsset] = []
        contract_type = (profile_hint or {}).get("contract_type") or "procurement_contract"
        threshold = self._extract_percent_threshold(source_text) or 30
        if "hard_rule" in selected_types:
            rule_id = "FIN-PUR-003" if contract_type == "procurement_contract" else "FIN-SVC-003"
            title = (
                f"采购预付款比例超过 {threshold}%"
                if contract_type == "procurement_contract"
                else f"服务预付款比例超过 {threshold}%"
            )
            drafts.append(
                self.create_asset_draft(
                    asset_type="hard_rule",
                    name=f"制度草稿：预付款比例超过 {threshold}% 控制",
                    applicability={"contract_type": contract_type},
                    content={
                        "rule_id": rule_id,
                        "title": title,
                        "level": "high" if contract_type == "procurement_contract" else "medium",
                        "conditions": [
                            {"fact_key": "payment.prepay_ratio", "operator": ">", "value": threshold}
                        ],
                        "evidence_fact_keys": ["payment.prepay_ratio"],
                        "policy_reference_ids": ["POLICY-PUR-002", "POLICY-FUND-006"]
                        if contract_type == "procurement_contract"
                        else ["POLICY-FUND-003"],
                        "reason_template": (
                            "合同约定预付款比例为 {payment.prepay_ratio}，"
                            "已超过配置资产 {asset_id} 设定的 {threshold}% 阈值。"
                        ),
                        "action_template": (
                            "将预付款比例降至 {threshold}% 以内，或补充例外审批说明。"
                        ),
                        "fact_key": "payment.prepay_ratio",
                        "operator": ">",
                        "value": threshold,
                        "risk_level": "high",
                        "policy_reference": source_text.strip()[:240],
                        "source_type": source_type,
                    },
                    schema_version="hard-rule-v1",
                    description="由制度文档生成的硬规则草稿，审核发布并绑定配置集后才会生效。",
                    actor=actor,
                )
            )
        if "semantic_rule" in selected_types:
            drafts.append(
                self.create_asset_draft(
                    asset_type="semantic_rule",
                    name="制度草稿：语义审查关注项",
                    applicability={"contract_type": contract_type},
                    content={
                        "prompt_template_id": "asset-prompt-semantic-rule-v1",
                        "output_schema": "semantic-rule-result-v1",
                        "policy_reference": source_text.strip()[:240],
                        "source_type": source_type,
                    },
                    schema_version="semantic-rule-v1",
                    description="由制度文档生成的语义规则草稿。",
                    actor=actor,
                )
            )
        if "risk_message_template" in selected_types:
            drafts.append(
                self.create_asset_draft(
                    asset_type="risk_message_template",
                    name="制度草稿：风险提示模板",
                    applicability={"contract_type": contract_type},
                    content={
                        "template": "{rule_title}: {reason} 建议：{action}",
                        "source_type": source_type,
                        "policy_reference": source_text.strip()[:240],
                    },
                    schema_version="risk-message-template-v1",
                    description="由制度文档生成的提示模板草稿。",
                    actor=actor,
                )
            )
        return {
            "drafts": drafts,
            "llm_execution": {
                "id": f"llm-draft-{uuid.uuid4().hex[:10]}",
                "purpose": "rule_draft",
                "status": "success",
                "model": "deterministic-draft-generator",
                "created_at": utc_now(),
                "source_type": source_type,
            },
        }

    def approve_asset(self, asset_id: str, *, actor: str = "reviewer", comment: str | None = None) -> ConfigAsset:
        return self._transition_asset(
            asset_id,
            expected={"draft"},
            status="approved",
            actor=actor,
            approval_comment=comment,
        )

    def reject_asset(self, asset_id: str, *, actor: str = "reviewer", comment: str | None = None) -> ConfigAsset:
        return self._transition_asset(
            asset_id,
            expected={"draft", "approved"},
            status="rejected",
            actor=actor,
            rejection_comment=comment,
        )

    def publish_asset(self, asset_id: str, *, actor: str = "reviewer") -> ConfigAsset:
        return self._transition_asset(
            asset_id,
            expected={"approved"},
            status="active",
            actor=actor,
            effective_from=utc_now(),
        )

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
        for ref in profile.assets:
            asset = next((item for item in assets if item.id == ref.asset_id), None)
            if not asset or asset.asset_type != "hard_rule" or asset.status != "active":
                continue
            hard_rules.append(
                {
                    "asset_id": asset.id,
                    "asset_version": asset.version,
                    **asset.content,
                }
            )
            threshold_condition = self._find_threshold_condition(asset)
            if threshold_condition is not None:
                threshold = float(threshold_condition)
                source_asset_id = asset.id
        return {
            "prepay_threshold": threshold,
            "prepay_threshold_asset_id": source_asset_id,
            "hard_rules": hard_rules,
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
        self._ensure_state_file()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            assets = [ConfigAsset.model_validate(item) for item in payload.get("assets", [])]
            profiles = [ReviewProfile.model_validate(item) for item in payload.get("profiles", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AssetStateError("配置资产仓库无法读取。") from exc
        assets, profiles = self._merge_seed_state(assets, profiles)
        return assets, profiles

    def _ensure_state_file(self) -> None:
        if self.state_path.exists():
            return
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._save_state(list(SEED_ASSETS), list(SEED_PROFILES))

    def _save_state(self, assets: list[ConfigAsset], profiles: list[ReviewProfile]) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {
            "assets": [asset.model_dump() for asset in assets],
            "profiles": [profile.model_dump() for profile in profiles],
        }
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)

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
