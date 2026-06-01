from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    AgentTraceEvent,
    Clause,
    DatabaseProbe,
    ExtractedField,
    LLMProbe,
    ObjectStorageProbe,
    RagflowProbe,
    ReportSnapshot,
    RiskFinding,
    TaskRecord,
    WorkflowStep,
)


CONTRACT_TYPE_LABELS = {
    "procurement_contract": "采购合同",
    "service_contract": "服务合同",
    "unknown_contract": "未识别合同",
}
OVERALL_RISK_LABELS = {"red": "高风险", "yellow": "中风险", "green": "低风险"}
STATUS_LABELS = {
    "pending_review": "待复核",
    "watchlist": "需关注",
    "ready": "建议通过",
}
DECISION_LABELS = {
    "manual_review": "人工复核",
    "review_recommended": "建议复核",
    "pass": "建议通过",
}
FIELD_LABELS = {
    "contract_name": "合同名称",
    "contract_type": "合同类型",
    "party_a_name": "甲方",
    "party_b_name": "乙方",
    "amount_total": "合同金额",
    "invoice.type": "发票类型",
    "invoice.tax_rate": "税率",
    "invoice.issue_timing": "开票时点",
    "payment.prepay_ratio": "预付款比例",
    "payment.final_ratio": "尾款比例",
    "payment.final_condition": "尾款条件",
    "acceptance.required": "是否存在验收条款",
    "warranty.present": "是否存在质保条款",
    "warranty.period_months": "质保期",
    "term.auto_renewal": "是否自动续约",
    "dispute.location": "争议解决地",
    "account.payee_name": "收款账户主体",
    "account.same_as_counterparty": "收款主体是否与签约乙方一致",
    "liability.reciprocal": "违约责任是否对等",
}
DISPLAY_FIELD_ORDER = [
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
    "term.auto_renewal",
    "account.payee_name",
    "account.same_as_counterparty",
    "dispute.location",
    "liability.reciprocal",
]
POLICY_TITLES = {
    "POLICY-FUND-002": "发票要求",
    "POLICY-FUND-003": "预付款控制",
    "POLICY-FUND-004": "验收与付款衔接",
    "POLICY-FUND-005": "收款账户核验",
    "POLICY-FUND-006": "高风险付款人工复核",
    "POLICY-PUR-001": "采购合同必备条款",
    "POLICY-PUR-002": "采购预付款控制",
    "POLICY-PUR-003": "验收要求",
    "POLICY-PUR-004": "质量保证要求",
    "POLICY-PUR-005": "收款账户一致性",
    "POLICY-PUR-006": "框架合同续签控制",
    "POLICY-REV-001": "审查目标",
    "POLICY-REV-002": "审查结论分级",
    "POLICY-REV-003": "证据要求",
    "POLICY-REV-004": "自动续约审查",
    "POLICY-REV-005": "争议解决条款审查",
}
TIMELINE = [
    "上传合同并创建任务",
    "解析条款并抽取关键字段",
    "执行财务合规规则初筛",
    "连接 RAGFlow 校验制度依据可用性",
    "输出风险卡片并进入人工复核",
]
CLAUSE_HEADER_RE = re.compile(r"^【(?P<id>[A-Z]\d{3})】(?P<title>.+?)\s*$", re.MULTILINE)


def analyze_contract(
    task_id: str,
    source_filename: str,
    contract_name: str | None,
    contract_text: str,
    created_at: str | None = None,
) -> TaskRecord:
    normalized_text = contract_text.replace("\r\n", "\n").strip()
    resolved_name = (contract_name or extract_contract_name(normalized_text) or Path(source_filename).stem).strip()
    clauses = parse_clauses(normalized_text)
    contract_type = detect_contract_type(resolved_name, normalized_text)
    facts = extract_facts(resolved_name, contract_type, clauses, normalized_text)
    risks = evaluate_rules(contract_type, facts)
    clauses = apply_clause_status(clauses, risks)
    extracted_fields = build_extracted_fields(contract_type, facts)
    overall_risk = derive_overall_risk(risks)
    status = derive_status(overall_risk)
    decision = derive_decision(overall_risk)
    created_at = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    workflow_steps = build_workflow_steps(created_at, status)
    agent_trace = build_agent_trace(created_at, contract_type, clauses, extracted_fields, risks, status)
    report_snapshot = build_report_snapshot(resolved_name, risks, decision)
    return TaskRecord(
        id=task_id,
        name=resolved_name,
        contract_type=contract_type,
        contract_type_label=CONTRACT_TYPE_LABELS[contract_type],
        source_filename=source_filename,
        status=status,
        status_label=STATUS_LABELS[status],
        overall_risk=overall_risk,
        overall_risk_label=OVERALL_RISK_LABELS[overall_risk],
        decision=decision,
        decision_label=DECISION_LABELS[decision],
        summary=build_summary(risks),
        created_at=created_at,
        contract_text=normalized_text,
        clauses=clauses,
        extracted_fields=extracted_fields,
        risks=risks,
        workflow_steps=workflow_steps,
        agent_trace=agent_trace,
        report_snapshot=report_snapshot,
    )


def build_dashboard_payload(
    tasks: list[TaskRecord],
    ragflow: RagflowProbe,
    llm: LLMProbe,
    database: DatabaseProbe | None = None,
    object_storage: ObjectStorageProbe | None = None,
) -> dict[str, Any]:
    pending_count = sum(task.status == "pending_review" for task in tasks)
    return {
        "headline": {
            "title": "合同合规审查工作台",
            "subtitle": "从合同文本、规则命中、制度依据到人工复核，先跑通一条可验证的真实任务链路。",
            "badges": ["真实任务持久化", "规则初筛已接入", "RAGFlow 探测已接入"],
        },
        "metrics": [
            {"label": "合同任务", "value": str(len(tasks)), "tone": "neutral"},
            {"label": "启用规则", "value": "7", "tone": "warn"},
            {"label": "关键字段", "value": str(len(DISPLAY_FIELD_ORDER)), "tone": "ok"},
            {"label": "待复核", "value": str(pending_count), "tone": "danger"},
        ],
        "tasks": [build_task_summary(task) for task in tasks],
        "timeline": TIMELINE,
        "ragflow": build_ragflow_payload(ragflow),
        "llm": build_llm_payload(llm),
        "database": build_database_payload(database),
        "object_storage": build_object_storage_payload(object_storage),
    }


def build_review_payload(
    task: TaskRecord,
    ragflow: RagflowProbe,
    llm: LLMProbe,
    database: DatabaseProbe | None = None,
    object_storage: ObjectStorageProbe | None = None,
) -> dict[str, Any]:
    high_count = sum(risk.level == "high" for risk in task.risks)
    medium_count = sum(risk.level == "medium" for risk in task.risks)
    top_rule = task.risks[0].rule_id if task.risks else "未命中"
    return {
        "task": {
            "id": task.id,
            "name": task.name,
            "status": task.status_label,
            "risk": task.overall_risk_label,
            "contract_type": task.contract_type_label,
            "state_class": overall_risk_to_state_class(task.overall_risk),
        },
        "summary_cards": [
            {"label": "总风险数", "value": str(len(task.risks))},
            {"label": "高风险", "value": str(high_count)},
            {"label": "中风险", "value": str(medium_count)},
            {"label": "首要规则", "value": top_rule},
        ],
        "clauses": [
            {"id": clause.id, "title": clause.title, "status": clause.status}
            for clause in task.clauses
        ],
        "contract_excerpt": [
            {
                "id": clause.id,
                "title": clause.title,
                "text": clause.text,
                "status": clause.status,
            }
            for clause in task.clauses
        ],
        "fields": [
            {
                "label": field.label,
                "value": field.value,
                "status": field.status,
            }
            for field in task.extracted_fields
        ],
        "risks": [
            {
                "level": risk.level,
                "title": risk.title,
                "rule": risk.rule_id,
                "reason": risk.reason,
                "evidence": "、".join(risk.evidence_clause_ids) or "未定位",
                "policy": " / ".join(render_policy_reference(policy_id) for policy_id in risk.policy_reference_ids),
                "action": risk.action,
            }
            for risk in task.risks
        ],
        "workflow_steps": [
            {
                "key": step.key,
                "label": step.label,
                "status": step.status,
                "updated_at": step.updated_at,
            }
            for step in task.workflow_steps
        ],
        "trace": [
            {
                "at": event.at,
                "type": event.type,
                "message": event.message,
            }
            for event in task.agent_trace[-8:]
        ],
        "report": (
            {
                "title": task.report_snapshot.title,
                "summary": task.report_snapshot.summary,
                "recommendation": task.report_snapshot.recommendation,
                "generated_at": task.report_snapshot.generated_at,
            }
            if task.report_snapshot
            else None
        ),
        "ragflow": build_ragflow_payload(ragflow),
        "llm": build_llm_payload(llm),
        "database": build_database_payload(database),
        "object_storage": build_object_storage_payload(object_storage),
    }


def build_task_summary(task: TaskRecord) -> dict[str, str]:
    return {
        "id": task.id,
        "name": task.name,
        "status": task.status_label,
        "risk": task.overall_risk_label,
        "risk_tone": overall_risk_to_chip_tone(task.overall_risk),
        "summary": task.summary,
        "created_at": task.created_at[:19].replace("T", " "),
    }


def build_ragflow_payload(ragflow: RagflowProbe) -> dict[str, Any]:
    tone = "ok" if ragflow.healthy else "warn"
    status_label = "已连接" if ragflow.healthy else "待处理"
    return {
        "base_url": ragflow.base_url,
        "detail": ragflow.detail,
        "datasets": ragflow.datasets,
        "tone": tone,
        "status_label": status_label,
    }


def build_llm_payload(llm: LLMProbe) -> dict[str, Any]:
    if llm.verified:
        tone = "ok"
        status_label = "已验证"
    elif llm.configured:
        tone = "warn"
        status_label = "已配置"
    else:
        tone = "danger"
        status_label = "待配置"
    return {
        "base_url": llm.base_url,
        "chat_completions_url": llm.chat_completions_url,
        "model": llm.model,
        "title": llm.title,
        "detail": llm.detail,
        "tone": tone,
        "status_label": status_label,
        "api_key_present": llm.api_key_present,
        "configured": llm.configured,
        "verified": llm.verified,
        "env_file_path": llm.env_file_path,
        "missing_fields": llm.missing_fields,
    }


def build_database_payload(database: DatabaseProbe | None) -> dict[str, Any]:
    if database is None:
        return {
            "backend": "unknown",
            "tone": "warn",
            "status_label": "未检查",
            "detail": "数据库状态尚未探测。",
            "dsn": None,
            "task_count": None,
        }
    return {
        "backend": database.backend,
        "tone": "ok" if database.healthy else "danger",
        "status_label": "已连接" if database.healthy else "待处理",
        "detail": database.detail,
        "dsn": database.dsn,
        "task_count": database.task_count,
    }


def build_object_storage_payload(object_storage: ObjectStorageProbe | None) -> dict[str, Any]:
    if object_storage is None:
        return {
            "backend": "unknown",
            "tone": "warn",
            "status_label": "未检查",
            "detail": "原件存储状态尚未探测。",
            "endpoint_url": None,
            "bucket": None,
        }
    return {
        "backend": object_storage.backend,
        "tone": "ok" if object_storage.healthy else "danger",
        "status_label": "已连接" if object_storage.healthy else "待处理",
        "detail": object_storage.detail,
        "endpoint_url": object_storage.endpoint_url,
        "bucket": object_storage.bucket,
    }


def parse_clauses(text: str) -> list[Clause]:
    matches = list(CLAUSE_HEADER_RE.finditer(text))
    if not matches:
        sections = [section.strip() for section in re.split(r"\n{2,}", text) if section.strip()]
        return [
            Clause(id=f"C{index:03d}", title=f"段落 {index}", text=section)
            for index, section in enumerate(sections, start=1)
        ]

    clauses: list[Clause] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        clauses.append(
            Clause(
                id=match.group("id").strip(),
                title=match.group("title").strip(),
                text=body,
            )
        )
    return clauses


def extract_contract_name(text: str) -> str | None:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def detect_contract_type(contract_name: str, text: str) -> str:
    if any(keyword in contract_name for keyword in ["服务合同", "服务"]):
        return "service_contract"
    if any(keyword in contract_name for keyword in ["采购合同", "采购"]):
        return "procurement_contract"

    joined = f"{contract_name}\n{text}"
    if any(keyword in joined for keyword in ["采购合同", "采购", "物资"]):
        return "procurement_contract"
    if any(keyword in joined for keyword in ["服务合同", "服务", "开发", "运维"]):
        return "service_contract"
    return "unknown_contract"


def extract_facts(
    contract_name: str,
    contract_type: str,
    clauses: list[Clause],
    text: str,
) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    set_fact(facts, "contract_name", contract_name)
    set_fact(facts, "contract_type", CONTRACT_TYPE_LABELS[contract_type])

    party_clause = first_clause_matching(clauses, ["甲方", "乙方", "合同双方"])
    party_a = search_text(r"甲方[:：]\s*([^\n]+)", party_clause.text if party_clause else text)
    party_b = search_text(r"乙方[:：]\s*([^\n]+)", party_clause.text if party_clause else text)
    set_fact(facts, "party_a_name", party_a, ids_of([party_clause]))
    set_fact(facts, "party_b_name", party_b, ids_of([party_clause]))

    amount_clause = first_clause_matching(clauses, ["合同金额", "合同总价", "总价"])
    amount = search_text(r"人民币\s*([\d,]+(?:\.\d+)?)\s*元", amount_clause.text if amount_clause else text)
    set_fact(facts, "amount_total", f"{amount} 元" if amount else None, ids_of([amount_clause]))

    invoice_clauses = clauses_matching(clauses, ["发票"])
    invoice_text = "\n".join(clause.text for clause in invoice_clauses)
    invoice_type = None
    if "增值税专用发票" in invoice_text:
        invoice_type = "增值税专用发票"
    elif "普通发票" in invoice_text:
        invoice_type = "普通发票"
    elif invoice_clauses:
        invoice_type = "已提及发票"
    tax_rate = search_text(r"(?:税率|税点)[^\d]{0,6}(\d{1,2}(?:\.\d+)?)%", invoice_text)
    if tax_rate is None:
        tax_rate = search_text(r"发票[^\n。]{0,20}(\d{1,2}(?:\.\d+)?)%", invoice_text)
    issue_timing = None
    if "付款后" in invoice_text:
        issue_timing = "付款后开票"
    elif "付款前" in invoice_text:
        issue_timing = "付款前开票"
    elif invoice_text:
        issue_timing = "合同已约定开票"
    set_fact(facts, "invoice.type", invoice_type, ids_of(invoice_clauses))
    set_fact(facts, "invoice.tax_rate", f"{tax_rate}%" if tax_rate else None, ids_of(invoice_clauses))
    set_fact(facts, "invoice.issue_timing", issue_timing, ids_of(invoice_clauses))

    payment_clauses = clauses_matching(clauses, ["付款", "支付", "结算"])
    payment_text = "\n".join(clause.text for clause in payment_clauses)
    prepay_ratio = (
        search_text(r"(?:支付|付款)[^。\n；;]{0,20}?(\d{1,2}(?:\.\d+)?)%\s*(?:作为)?预付款", payment_text)
        or search_text(r"(\d{1,2}(?:\.\d+)?)%\s*(?:作为)?预付款", payment_text)
        or search_text(r"预付款[^。\n；;]{0,12}?(\d{1,2}(?:\.\d+)?)%", payment_text)
    )
    final_ratio = search_text(r"剩余\s*(\d{1,2}(?:\.\d+)?)%", payment_text)
    final_condition = None
    if any(keyword in payment_text for keyword in ["验收", "到货", "交付"]):
        final_condition = "尾款与到货/验收节点绑定"
    set_fact(
        facts,
        "payment.prepay_ratio",
        f"{trim_number(prepay_ratio)}%" if prepay_ratio else None,
        ids_of(payment_clauses),
    )
    derived_final_ratio = final_ratio
    if final_ratio is None and prepay_ratio is not None:
        derived_final_ratio = str(100 - int(float(prepay_ratio)))
    set_fact(
        facts,
        "payment.final_ratio",
        f"{trim_number(derived_final_ratio)}%" if derived_final_ratio else None,
        ids_of(payment_clauses),
    )
    set_fact(facts, "payment.final_condition", final_condition, ids_of(payment_clauses))

    acceptance_clause = first_clause_matching(clauses, ["验收", "签收", "到货"])
    acceptance_required = bool(
        acceptance_clause
        and any(keyword in f"{acceptance_clause.title}\n{acceptance_clause.text}" for keyword in ["验收", "签收"])
    )
    set_fact(facts, "acceptance.required", acceptance_required, ids_of([acceptance_clause]))

    warranty_clause = first_clause_matching(clauses, ["质保", "保修", "售后"])
    warranty_period = search_text(r"质保\s*(\d+)\s*个?月", warranty_clause.text if warranty_clause else "")
    set_fact(facts, "warranty.present", bool(warranty_clause), ids_of([warranty_clause]))
    set_fact(
        facts,
        "warranty.period_months",
        f"{warranty_period} 个月" if warranty_period else None,
        ids_of([warranty_clause]),
    )

    renewal_clause = first_clause_matching(clauses, ["续约", "顺延", "续签"])
    renewal_text = renewal_clause.text if renewal_clause else text
    auto_renewal = any(keyword in renewal_text for keyword in ["自动续约", "自动顺延", "默认顺延", "自动续展"])
    approval_required = any(keyword in renewal_text for keyword in ["审批", "另行签署", "书面协议", "重新签署"])
    set_fact(facts, "term.auto_renewal", auto_renewal, ids_of([renewal_clause]))

    dispute_clause = first_clause_matching(clauses, ["争议", "仲裁", "法院"])
    dispute_location = None
    if dispute_clause:
        dispute_location = search_text(
            r"(?:提交|由|向)(.+?)(?:人民法院|仲裁委员会)",
            dispute_clause.text,
        )
        if dispute_location:
            dispute_location = dispute_location.strip()
    set_fact(facts, "dispute.location", dispute_location, ids_of([dispute_clause]))
    set_fact(facts, "dispute.raw", dispute_clause.text if dispute_clause else None, ids_of([dispute_clause]))

    account_clause = first_clause_matching(clauses, ["收款账户", "账户名称", "开户名", "开户名称"])
    account_text = account_clause.text if account_clause else text
    payee_name = (
        search_text(r"账户名称[:：]\s*([^\n]+)", account_text)
        or search_text(r"开户名[:：]\s*([^\n]+)", account_text)
        or search_text(r"开户名称[:：]\s*([^\n]+)", account_text)
    )
    same_as_counterparty = None
    if payee_name and party_b:
        same_as_counterparty = normalize_name(payee_name) == normalize_name(party_b)
    set_fact(facts, "account.payee_name", payee_name, ids_of([account_clause]))
    set_fact(
        facts,
        "account.same_as_counterparty",
        same_as_counterparty,
        ids_of([account_clause, party_clause]),
    )
    set_fact(facts, "approval.exception_required", approval_required, ids_of([renewal_clause]))

    liability_clause = first_clause_matching(clauses, ["违约责任", "赔偿", "责任限制"])
    liability_text = liability_clause.text if liability_clause else ""
    liability_reciprocal = None
    if liability_text:
        liability_reciprocal = not (
            "乙方承担的违约赔偿责任以其已收取的服务费为限" in liability_text
            and any(keyword in liability_text for keyword in ["甲方如逾期付款", "全部损失", "预期收益损失", "预期收益"])
        )
    set_fact(facts, "liability.reciprocal", liability_reciprocal, ids_of([liability_clause]))
    return facts


def evaluate_rules(
    contract_type: str,
    facts: dict[str, dict[str, Any]],
) -> list[RiskFinding]:
    risks: list[RiskFinding] = []

    invoice_type = fact_value(facts, "invoice.type")
    tax_rate = fact_value(facts, "invoice.tax_rate")
    prepay_ratio = parse_percent(fact_value(facts, "payment.prepay_ratio"))
    acceptance_required = bool(fact_value(facts, "acceptance.required"))
    payee_matches = fact_value(facts, "account.same_as_counterparty")
    auto_renewal = bool(fact_value(facts, "term.auto_renewal"))
    approval_required = bool(fact_value(facts, "approval.exception_required"))
    warranty_present = bool(fact_value(facts, "warranty.present"))
    dispute_raw = str(fact_value(facts, "dispute.raw") or "")
    liability_reciprocal = fact_value(facts, "liability.reciprocal")

    if not invoice_type:
        rule_id = "FIN-PUR-001" if contract_type == "procurement_contract" else "FIN-SVC-001"
        policy_ids = ["POLICY-PUR-001", "POLICY-FUND-002"] if contract_type == "procurement_contract" else ["POLICY-FUND-002"]
        risks.append(
            build_risk(
                rule_id=rule_id,
                title="缺失发票条款",
                level="medium",
                reason="合同文本中未发现明确发票类型、开票方式或开票要求，后续付款审核口径不完整。",
                evidence_ids=[],
                policy_ids=policy_ids,
                action="补充发票类型、开票时点和发票内容，确保合同与付款资料一致。",
            )
        )

    if invoice_type and not tax_rate:
        rule_id = "FIN-PUR-002" if contract_type == "procurement_contract" else "FIN-SVC-002"
        risks.append(
            build_risk(
                rule_id=rule_id,
                title="缺失税率条款",
                level="medium",
                reason="合同提到了发票，但未写明税率或税点，税务处理和付款审核依据不足。",
                evidence_ids=fact_evidence_ids(facts, "invoice.type"),
                policy_ids=["POLICY-FUND-002"],
                action="补充明确税率、发票类型及开票内容，避免后续开票与付款口径不一致。",
            )
        )

    if prepay_ratio is not None and prepay_ratio > 30:
        if contract_type == "procurement_contract":
            risks.append(
                build_risk(
                    rule_id="FIN-PUR-003",
                    title="采购预付款比例超过 30%",
                    level="high",
                    reason=f"合同约定预付款比例为 {trim_number(str(prepay_ratio))}%，已超过采购管理制度建议阈值。",
                    evidence_ids=fact_evidence_ids(facts, "payment.prepay_ratio"),
                    policy_ids=["POLICY-PUR-002", "POLICY-FUND-006"],
                    action="将预付款比例降至 30% 以内，或补充采购与财务的例外审批说明。",
                )
            )
        elif acceptance_required:
            risks.append(
                build_risk(
                    rule_id="FIN-SVC-004",
                    title="高预付款且尾款依赖验收",
                    level="high",
                    reason=f"服务合同预付款比例为 {trim_number(str(prepay_ratio))}%，且尾款与交付/验收节点绑定，资金前置风险较高。",
                    evidence_ids=merge_ids(
                        fact_evidence_ids(facts, "payment.prepay_ratio"),
                        fact_evidence_ids(facts, "acceptance.required"),
                    ),
                    policy_ids=["POLICY-FUND-003", "POLICY-FUND-004", "POLICY-FUND-006"],
                    action="压低预付款比例，并增加阶段性交付与验收控制，必要时进入联合复核。",
                )
            )
        else:
            risks.append(
                build_risk(
                    rule_id="FIN-SVC-003",
                    title="服务预付款比例超过 30%",
                    level="medium",
                    reason=f"服务合同预付款比例为 {trim_number(str(prepay_ratio))}%，已超过制度建议阈值。",
                    evidence_ids=fact_evidence_ids(facts, "payment.prepay_ratio"),
                    policy_ids=["POLICY-FUND-003"],
                    action="建议补充例外审批依据，或将预付款比例调整至 30% 以内。",
                )
            )

    if not acceptance_required:
        rule_id = "FIN-PUR-004" if contract_type == "procurement_contract" else "FIN-SVC-005"
        policy_ids = ["POLICY-PUR-003"] if contract_type == "procurement_contract" else ["POLICY-FUND-004"]
        risks.append(
            build_risk(
                rule_id=rule_id,
                title="缺失验收条款",
                level="medium",
                reason="合同中未发现明确验收、交付成果确认或到货验收安排，后续尾款支付依据不足。",
                evidence_ids=[],
                policy_ids=policy_ids,
                action="补充明确的验收标准、验收主体和验收节点，避免以默认认可替代关键确认。",
            )
        )

    if payee_matches is False:
        rule_id = "FIN-PUR-005" if contract_type == "procurement_contract" else "FIN-SVC-009"
        policy_ids = ["POLICY-FUND-005"]
        if contract_type == "procurement_contract":
            policy_ids.append("POLICY-PUR-005")
        risks.append(
            build_risk(
                rule_id=rule_id,
                title="收款账户主体与签约乙方不一致",
                level="high",
                reason="合同乙方与收款账户名称不一致，存在代收款、关联方收款或账户变更未留痕的风险。",
                evidence_ids=fact_evidence_ids(facts, "account.same_as_counterparty"),
                policy_ids=policy_ids,
                action="要求提供专项审批和账户变更证明，或改回与签约主体一致的收款账户。",
            )
        )

    if auto_renewal and not approval_required:
        rule_id = "FIN-PUR-007" if contract_type == "procurement_contract" else "FIN-SVC-006"
        policy_ids = ["POLICY-REV-004"]
        if contract_type == "procurement_contract":
            policy_ids.append("POLICY-PUR-006")
        risks.append(
            build_risk(
                rule_id=rule_id,
                title="自动续约缺少审批前提",
                level="high",
                reason="合同包含自动续约或默认顺延安排，但未看到重新审批、书面续签或授权边界控制。",
                evidence_ids=fact_evidence_ids(facts, "term.auto_renewal"),
                policy_ids=policy_ids,
                action="改为到期后重新审批并签署书面续约文件，不建议使用默认自动顺延。",
            )
        )

    if contract_type == "service_contract" and dispute_raw and "乙方所在地" in dispute_raw:
        risks.append(
            build_risk(
                rule_id="FIN-SVC-007",
                title="争议解决地约定偏向乙方",
                level="medium",
                reason="争议解决条款将仲裁或诉讼安排在乙方所在地，后续维权和举证成本偏高。",
                evidence_ids=fact_evidence_ids(facts, "dispute.location"),
                policy_ids=["POLICY-REV-005"],
                action="建议改为甲方所在地法院或双方均可接受的争议解决地。",
            )
        )

    if contract_type == "service_contract" and liability_reciprocal is False:
        risks.append(
            build_risk(
                rule_id="FIN-SVC-008",
                title="违约责任明显不对等",
                level="high",
                reason="合同对乙方责任设置了明显上限，但对甲方迟延付款责任扩张至全部损失和预期收益，责任分配失衡。",
                evidence_ids=fact_evidence_ids(facts, "liability.reciprocal"),
                policy_ids=["POLICY-REV-001"],
                action="将双方违约责任调整为对等口径，并统一责任边界或赔偿上限。",
            )
        )

    if contract_type == "procurement_contract" and not warranty_present:
        risks.append(
            build_risk(
                rule_id="FIN-PUR-006",
                title="缺失质保或保修条款",
                level="medium",
                reason="采购合同中未看到明确质保期限、保修责任或售后承诺，后续设备问题缺少追责抓手。",
                evidence_ids=[],
                policy_ids=["POLICY-PUR-004"],
                action="补充质保期限、保修范围、响应时限与更换维修责任。",
            )
        )

    risks.sort(key=lambda item: risk_priority(item.level), reverse=True)
    return risks


def build_risk(
    rule_id: str,
    title: str,
    level: str,
    reason: str,
    evidence_ids: list[str],
    policy_ids: list[str],
    action: str,
) -> RiskFinding:
    return RiskFinding(
        rule_id=rule_id,
        title=title,
        level=level,
        message=title,
        reason=reason,
        evidence_clause_ids=evidence_ids,
        policy_reference_ids=policy_ids + ["POLICY-REV-003"],
        action=action,
    )


def build_extracted_fields(
    contract_type: str,
    facts: dict[str, dict[str, Any]],
) -> list[ExtractedField]:
    fields: list[ExtractedField] = []
    for key in DISPLAY_FIELD_ORDER:
        value = fact_value(facts, key)
        if key == "contract_type":
            value = CONTRACT_TYPE_LABELS.get(contract_type, "未识别合同")
        fields.append(
            ExtractedField(
                key=key,
                label=FIELD_LABELS[key],
                value=format_field_value(value),
                status=fact_status(facts, key),
                evidence_clause_ids=fact_evidence_ids(facts, key),
            )
        )
    return fields


def apply_clause_status(clauses: list[Clause], risks: list[RiskFinding]) -> list[Clause]:
    clause_risk: dict[str, str] = {}
    for risk in risks:
        tone = "danger" if risk.level == "high" else "warn"
        for clause_id in risk.evidence_clause_ids:
            current = clause_risk.get(clause_id)
            if current == "danger":
                continue
            clause_risk[clause_id] = tone
    updated: list[Clause] = []
    for clause in clauses:
        updated.append(
            clause.model_copy(update={"status": clause_risk.get(clause.id, "ok")})
        )
    return updated


def derive_overall_risk(risks: list[RiskFinding]) -> str:
    if any(risk.level == "high" for risk in risks):
        return "red"
    if any(risk.level == "medium" for risk in risks):
        return "yellow"
    return "green"


def derive_status(overall_risk: str) -> str:
    if overall_risk == "red":
        return "pending_review"
    if overall_risk == "yellow":
        return "watchlist"
    return "ready"


def derive_decision(overall_risk: str) -> str:
    if overall_risk == "red":
        return "manual_review"
    if overall_risk == "yellow":
        return "review_recommended"
    return "pass"


def build_summary(risks: list[RiskFinding]) -> str:
    if not risks:
        return "未命中高关注规则，可进入人工抽样复核。"
    top = risks[0]
    return f"命中 {len(risks)} 条风险，优先关注“{top.title}”。"


def first_clause_matching(clauses: list[Clause], keywords: list[str]) -> Clause | None:
    for clause in clauses:
        if any(keyword in clause.title or keyword in clause.text for keyword in keywords):
            return clause
    return None


def clauses_matching(clauses: list[Clause], keywords: list[str]) -> list[Clause]:
    return [
        clause
        for clause in clauses
        if any(keyword in clause.title or keyword in clause.text for keyword in keywords)
    ]


def ids_of(clauses: list[Clause | None]) -> list[str]:
    return [clause.id for clause in clauses if clause is not None]


def merge_ids(*id_groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in id_groups:
        for item in group:
            if item not in merged:
                merged.append(item)
    return merged


def search_text(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1).strip()


def normalize_name(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value or "")


def set_fact(
    facts: dict[str, dict[str, Any]],
    key: str,
    value: Any,
    evidence_clause_ids: list[str] | None = None,
) -> None:
    status = "missing" if value is None or value == "" else "present"
    facts[key] = {
        "value": value,
        "status": status,
        "evidence_clause_ids": evidence_clause_ids or [],
    }


def fact_value(facts: dict[str, dict[str, Any]], key: str) -> Any:
    return facts.get(key, {}).get("value")


def fact_status(facts: dict[str, dict[str, Any]], key: str) -> str:
    return facts.get(key, {}).get("status", "missing")


def fact_evidence_ids(facts: dict[str, dict[str, Any]], key: str) -> list[str]:
    return list(facts.get(key, {}).get("evidence_clause_ids", []))


def format_field_value(value: Any) -> str:
    if value is None or value == "":
        return "未提取"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def parse_percent(value: Any) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)", str(value))
    if not match:
        return None
    return int(match.group(1))


def trim_number(value: str | None) -> str:
    if value is None:
        return ""
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    return f"{as_float:.1f}"


def risk_priority(level: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(level, 0)


def overall_risk_to_chip_tone(overall_risk: str) -> str:
    return {"red": "high", "yellow": "medium", "green": "low"}[overall_risk]


def overall_risk_to_state_class(overall_risk: str) -> str:
    return {"red": "state-danger", "yellow": "state-warn", "green": "state-ok"}[overall_risk]


def render_policy_reference(policy_id: str) -> str:
    title = POLICY_TITLES.get(policy_id)
    if title:
        return f"{policy_id} {title}"
    return policy_id


def build_workflow_steps(created_at: str, status: str) -> list[WorkflowStep]:
    review_status = "waiting" if status == "pending_review" else "done"
    return [
        WorkflowStep(key="uploaded", label="原件存档", status="done", updated_at=created_at),
        WorkflowStep(key="parsing", label="条款解析", status="done", updated_at=created_at),
        WorkflowStep(key="extracting", label="事实抽取", status="done", updated_at=created_at),
        WorkflowStep(key="evaluating", label="规则裁决", status="done", updated_at=created_at),
        WorkflowStep(key="review", label="人工复核", status=review_status, updated_at=created_at),
        WorkflowStep(key="report", label="报告快照", status="done", updated_at=created_at),
    ]


def build_agent_trace(
    created_at: str,
    contract_type: str,
    clauses: list[Clause],
    extracted_fields: list[ExtractedField],
    risks: list[RiskFinding],
    status: str,
) -> list[AgentTraceEvent]:
    awaiting_review = status == "pending_review"
    return [
        AgentTraceEvent(
            at=created_at,
            type="agent.plan",
            message="生成审查计划，确定先执行条款解析、字段抽取和规则评估。",
            payload={"contract_type": contract_type},
        ),
        AgentTraceEvent(
            at=created_at,
            type="document.parse",
            message=f"完成条款解析，生成 {len(clauses)} 个可审计条款片段。",
            payload={"clause_count": len(clauses)},
        ),
        AgentTraceEvent(
            at=created_at,
            type="fact.extract",
            message=f"完成关键事实抽取，当前输出 {len(extracted_fields)} 个字段。",
            payload={"field_count": len(extracted_fields)},
        ),
        AgentTraceEvent(
            at=created_at,
            type="rule.evaluate",
            message=f"规则引擎完成初筛，命中 {len(risks)} 条风险。",
            payload={"risk_count": len(risks)},
        ),
        AgentTraceEvent(
            at=created_at,
            type="review.route",
            message="由于存在高/中风险，任务已进入人工复核队列。"
            if awaiting_review
            else "当前任务未命中高风险，已可直接进入报告确认。",
            payload={"status": status},
        ),
    ]


def build_report_snapshot(name: str, risks: list[RiskFinding], decision: str) -> ReportSnapshot:
    high_count = sum(risk.level == "high" for risk in risks)
    medium_count = sum(risk.level == "medium" for risk in risks)
    return ReportSnapshot(
        title=f"{name} 审查报告快照",
        summary=f"系统识别 {len(risks)} 条风险，其中高风险 {high_count} 条、中风险 {medium_count} 条。",
        recommendation=build_report_recommendation(risks, decision),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def build_report_recommendation(risks: list[RiskFinding], decision: str) -> str:
    if not risks:
        return "建议完成人工抽样复核后进入签署或执行流程。"
    if decision == "manual_review":
        return "建议审查人优先处理高风险项，补齐依据材料后再确认是否放行。"
    if decision == "review_recommended":
        return "建议结合业务背景复核中风险项，必要时补充条款和审批依据。"
    return "当前审查结果可作为后续报告与归档的基础版本。"
