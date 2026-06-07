from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
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
from app.services.llm import LLMClient


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
    "review_completed": "复核完成",
    "final_approved": "复核通过",
    "returned_for_materials": "退回补材料",
    "revision_required": "要求整改",
    "archived": "已归档",
}
DECISION_LABELS = {
    "manual_review": "人工复核",
    "review_recommended": "建议复核",
    "pass": "建议通过",
    "revision_required": "建议整改",
    "conditional_pass": "附条件通过",
    "final_approved": "复核通过",
    "returned_for_materials": "退回补材料",
    "archived": "归档完成",
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
WORKFLOW_STEP_ORDER = ["uploaded", "parsing", "extracting", "evaluating", "semantic_rules", "review", "report"]


class ReviewRunExecutor:
    def __init__(
        self,
        *,
        task_id: str,
        source_filename: str,
        contract_name: str | None,
        contract_text: str,
        created_at: str | None = None,
        rule_context: dict[str, Any] | None = None,
        resume_from_step: str | None = None,
        existing_checkpoints: list[dict[str, Any]] | None = None,
    ) -> None:
        self.task_id = task_id
        self.source_filename = source_filename
        self.contract_name = contract_name
        self.normalized_text = contract_text.replace("\r\n", "\n").strip()
        self.created_at = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.rule_context = rule_context
        self.resume_from_step = resume_from_step
        self.existing_checkpoints = {
            item.get("step_key"): item
            for item in (existing_checkpoints or [])
            if isinstance(item, dict) and item.get("step_key")
        }
        self.checkpoint_events: list[AgentTraceEvent] = []

    def run(self) -> TaskRecord:
        resolved_name = (
            self.contract_name
            or extract_contract_name(self.normalized_text)
            or Path(self.source_filename).stem
        ).strip()
        self._checkpoint("uploaded", "succeeded", f"Source file {self.source_filename} archived.")

        clauses = parse_clauses(self.normalized_text, rule_context=self.rule_context)
        fallback_used = any(clause.parser_source.startswith("fallback:") for clause in clauses)
        self._checkpoint(
            "parsing",
            "succeeded_with_warnings" if fallback_used else "succeeded",
            f"Parsed {len(clauses)} clauses; fallback_used={fallback_used}.",
            {"clause_count": len(clauses), "fallback_used": fallback_used},
        )

        contract_type = detect_contract_type(resolved_name, self.normalized_text)
        facts = extract_facts(resolved_name, contract_type, clauses, self.normalized_text, rule_context=self.rule_context)
        apply_llm_extraction_fallback(facts, clauses, self.normalized_text, contract_type, rule_context=self.rule_context)
        candidate_count = sum(1 for fact in facts.values() if fact.get("status") == "candidate")
        self._checkpoint(
            "extracting",
            "succeeded",
            f"Extracted {len(facts)} facts; llm_candidates={candidate_count}.",
            {"fact_count": len(facts), "llm_candidate_count": candidate_count},
        )

        risks = evaluate_rules(contract_type, facts, rule_context=self.rule_context)
        self._checkpoint(
            "evaluating",
            "succeeded",
            f"Evaluated deterministic rules; risks={len(risks)}.",
            {"risk_count": len(risks)},
        )

        semantic_results = evaluate_semantic_rules(
            contract_type,
            facts,
            clauses,
            self.normalized_text,
            rule_context=self.rule_context,
        )
        semantic_warning_statuses = {"failed", "fallback", "low_confidence", "missing_evidence"}
        semantic_warning_count = sum(
            1 for result in semantic_results if result.get("status") in semantic_warning_statuses
        )
        self._checkpoint(
            "semantic_rules",
            "succeeded_with_warnings" if semantic_warning_count else "succeeded",
            f"Semantic rules completed; warnings={semantic_warning_count}.",
            {"semantic_rule_count": len(semantic_results), "warning_count": semantic_warning_count},
        )

        for result in semantic_results:
            semantic_risk = result.get("risk")
            if isinstance(semantic_risk, RiskFinding) and not risk_is_duplicate(semantic_risk, risks):
                risks.append(semantic_risk)
        risks = apply_risk_message_templates(risks, rule_context=self.rule_context)
        risks.sort(key=lambda item: risk_priority(item.level), reverse=True)
        clauses = apply_clause_status(clauses, risks)
        extracted_fields = build_extracted_fields(contract_type, facts, rule_context=self.rule_context)
        overall_risk = derive_overall_risk(risks, rule_context=self.rule_context)
        status = derive_status(overall_risk, rule_context=self.rule_context)
        decision = derive_decision(overall_risk)
        self._checkpoint(
            "review",
            "waiting_human" if status == "pending_review" else "succeeded",
            f"Routed to task status {status}.",
            {"status": status, "overall_risk": overall_risk, "decision": decision},
        )

        report_snapshot = build_report_snapshot(resolved_name, risks, decision, rule_context=self.rule_context)
        self._checkpoint(
            "report",
            "succeeded",
            f"Generated report snapshot v{report_snapshot.version}.",
            {"report_version": report_snapshot.version, "section_count": len(report_snapshot.sections)},
        )
        workflow_steps = build_workflow_steps(self.created_at, status, clauses=clauses, semantic_results=semantic_results)
        agent_trace = self.checkpoint_events + build_agent_trace(
            self.created_at,
            contract_type,
            clauses,
            extracted_fields,
            risks,
            status,
            semantic_results=semantic_results,
        )
        return TaskRecord(
            id=self.task_id,
            name=resolved_name,
            contract_type=contract_type,
            contract_type_label=CONTRACT_TYPE_LABELS[contract_type],
            source_filename=self.source_filename,
            status=status,
            status_label=STATUS_LABELS[status],
            overall_risk=overall_risk,
            overall_risk_label=OVERALL_RISK_LABELS[overall_risk],
            decision=decision,
            decision_label=DECISION_LABELS[decision],
            summary=build_summary(risks),
            created_at=self.created_at,
            contract_text=self.normalized_text,
            clauses=clauses,
            extracted_fields=extracted_fields,
            risks=risks,
            workflow_steps=workflow_steps,
            agent_trace=agent_trace,
            report_snapshot=report_snapshot,
        )

    def _checkpoint(
        self,
        step_key: str,
        status: str,
        output_summary: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "step_key": step_key,
            "status": status,
            "output_summary": output_summary,
            "resume_mode": "checkpoint",
            "resume_from_step": self.resume_from_step,
            "execution_mode": self._execution_mode_for_step(step_key),
            "reused_checkpoint": self._can_reuse_checkpoint(step_key),
        }
        if extra_payload:
            payload.update(extra_payload)
        self.checkpoint_events.append(
            AgentTraceEvent(
                at=self.created_at,
                type="workflow.checkpoint",
                message=(
                    f"Checkpoint reused for {step_key}."
                    if payload["reused_checkpoint"]
                    else f"Checkpoint saved for {step_key}."
                ),
                payload=payload,
            )
        )

    def _execution_mode_for_step(self, step_key: str) -> str:
        if self._can_reuse_checkpoint(step_key):
            return "checkpoint_reused"
        if self.resume_from_step:
            return "resumed_execution"
        return "fresh_execution"

    def _can_reuse_checkpoint(self, step_key: str) -> bool:
        if not self.resume_from_step or step_key not in self.existing_checkpoints:
            return False
        try:
            return WORKFLOW_STEP_ORDER.index(step_key) < WORKFLOW_STEP_ORDER.index(self.resume_from_step)
        except ValueError:
            return False


def analyze_contract(
    task_id: str,
    source_filename: str,
    contract_name: str | None,
    contract_text: str,
    created_at: str | None = None,
    rule_context: dict[str, Any] | None = None,
    resume_from_step: str | None = None,
    existing_checkpoints: list[dict[str, Any]] | None = None,
) -> TaskRecord:
    return ReviewRunExecutor(
        task_id=task_id,
        source_filename=source_filename,
        contract_name=contract_name,
        contract_text=contract_text,
        created_at=created_at,
        rule_context=rule_context,
        resume_from_step=resume_from_step,
        existing_checkpoints=existing_checkpoints,
    ).run()


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
    report_snapshots: list[dict] | None = None,
    workflow_run: Any | None = None,
) -> dict[str, Any]:
    high_count = sum(risk.level == "high" for risk in task.risks)
    resolved_count = sum(risk.review_status in {"confirmed", "rejected", "revised"} for risk in task.risks)
    pending_count = len(task.risks) - resolved_count
    top_rule = task.risks[0].rule_id if task.risks else "未命中"
    policy_titles = policy_reference_titles_from_snapshot(task.selected_profile_snapshot)
    return {
        "task": {
            "id": task.id,
            "name": task.name,
            "status": task.status_label,
            "risk": task.overall_risk_label,
            "decision": task.decision_label,
            "contract_type": task.contract_type_label,
            "state_class": overall_risk_to_state_class(task.overall_risk),
            "selected_profile_id": task.selected_profile_id,
            "selected_profile_name": task.selected_profile_name or "基础通用合同审查",
        },
        "profile": build_profile_payload(task),
        "task_decision": build_task_decision_payload(task),
        "task_decision_actions": [
            {
                "action_type": "approve",
                "label": "提交通过",
                "hint": "用于低风险或已取得例外审批的合同。",
            },
            {
                "action_type": "return_materials",
                "label": "退回补材料",
                "hint": "业务侧需补充审批、说明或证明材料。",
            },
            {
                "action_type": "require_revision",
                "label": "要求整改",
                "hint": "合同条款需修改后再提交复核。",
            },
            {
                "action_type": "archive",
                "label": "归档",
                "hint": "审查结论和报告已确认，进入留档状态。",
            },
        ],
        "summary_cards": [
            {"label": "总风险数", "value": str(len(task.risks))},
            {"label": "高风险", "value": str(high_count)},
            {"label": "待处理", "value": str(pending_count)},
            {"label": "首要规则", "value": top_rule},
        ],
        "clauses": [
            {
                "id": clause.id,
                "title": clause.title,
                "status": clause.status,
                "parser_source": clause.parser_source,
                "parser_template_id": clause.parser_template_id,
            }
            for clause in task.clauses
        ],
        "contract_excerpt": [
            {
                "id": clause.id,
                "title": clause.title,
                "text": clause.text,
                "status": clause.status,
                "parser_source": clause.parser_source,
                "parser_template_id": clause.parser_template_id,
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
                "message": risk.message,
                "rule": risk.rule_id,
                "source": risk_source(risk),
                "source_label": risk_source_label(risk),
                "reason": risk.reason,
                "evidence": "、".join(risk.evidence_clause_ids) or "未定位",
                "policy": " / ".join(render_policy_reference(policy_id, policy_titles=policy_titles) for policy_id in risk.policy_reference_ids),
                "action": risk.action,
                "review_status": risk.review_status,
                "review_status_label": render_review_status(risk.review_status),
                "reviewer_comment": risk.reviewer_comment,
            }
            for risk in task.risks
        ],
        "review_actions": [
            {
                "id": action.id,
                "target_type": action.target_type,
                "target_id": action.target_id,
                "action_type": action.action_type,
                "action_label": render_review_action(action.action_type),
                "actor": action.actor,
                "comment": action.comment,
                "created_at": action.created_at,
            }
            for action in sorted(task.review_actions, key=lambda item: item.created_at, reverse=True)
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
        "workflow_run": (
            {
                "id": workflow_run.id,
                "status": workflow_run.status,
                "run_type": workflow_run.run_type,
                "source": workflow_run.source,
                "input_hash": workflow_run.input_hash,
                "started_at": workflow_run.started_at,
                "finished_at": workflow_run.finished_at,
                "retry_count": workflow_run.retry_count,
                "resume_from_step": workflow_run.metadata.get("resume_from_step"),
                "checkpoint_count": workflow_run.metadata.get("checkpoint_count", 0),
                "reused_checkpoint_count": workflow_run.metadata.get("reused_checkpoint_count", 0),
                "reused_checkpoint_steps": workflow_run.metadata.get("reused_checkpoint_steps", []),
                "execution_plan": workflow_run.metadata.get("execution_plan", []),
                "worker_mode": workflow_run.metadata.get("worker_mode"),
                "worker_status": workflow_run.metadata.get("worker_status"),
                "worker_results": workflow_run.metadata.get("worker_results", []),
                "can_resume": not task.review_actions,
                "step_runs": [
                    {
                        "id": step.id,
                        "step_key": step.step_key,
                        "label": step.label,
                        "status": step.status,
                        "input_hash": step.input_hash,
                        "output_summary": step.output_summary,
                        "error": step.error,
                        "retry_count": step.retry_count,
                        "can_retry": step.step_key in {"parsing", "extracting", "evaluating", "semantic_rules"}
                        and not task.review_actions,
                        "checkpoint_saved": bool(step.metadata.get("checkpoint_saved")),
                        "checkpoint_status": step.metadata.get("checkpoint_status"),
                        "resume_mode": step.metadata.get("resume_mode"),
                        "resume_from_step": step.metadata.get("resume_from_step"),
                        "execution_mode": step.metadata.get("execution_mode"),
                        "reused_checkpoint": bool(step.metadata.get("reused_checkpoint")),
                        "updated_at": step.updated_at,
                    }
                    for step in workflow_run.step_runs
                ],
            }
            if workflow_run
            else None
        ),
        "trace": [
            {
                "at": event.at,
                "type": event.type,
                "message": event.message,
                "payload": event.payload,
            }
            for event in task.agent_trace[-8:]
        ],
        "report": (
            {
                "title": task.report_snapshot.title,
                "summary": task.report_snapshot.summary,
                "recommendation": task.report_snapshot.recommendation,
                "sections": task.report_snapshot.sections,
                "generated_at": task.report_snapshot.generated_at,
                "version": task.report_snapshot.version,
                "report_type": task.report_snapshot.report_type,
                "report_type_label": task.report_snapshot.report_type_label,
                "generated_by": task.report_snapshot.generated_by,
                "file_sha256": task.report_snapshot.file_sha256,
                "file_path": task.report_snapshot.file_path,
            }
            if task.report_snapshot
            else None
        ),
        "report_history": build_report_history_payload(report_snapshots or []),
        "ragflow": build_ragflow_payload(ragflow),
        "llm": build_llm_payload(llm),
        "database": build_database_payload(database),
        "object_storage": build_object_storage_payload(object_storage),
    }


def build_report_history_payload(report_snapshots: list[dict]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for snapshot in report_snapshots:
        report_type = snapshot.get("report_type") or "process_snapshot"
        history.append(
            {
                "version": snapshot.get("version"),
                "title": snapshot.get("title"),
                "report_type": report_type,
                "report_type_label": "交付报告" if report_type == "delivery_report" else "过程快照",
                "generated_by": snapshot.get("generated_by") or "system",
                "generated_at": snapshot.get("generated_at"),
                "file_sha256": snapshot.get("file_sha256"),
                "file_path": snapshot.get("file_path"),
            }
        )
    return history


def build_task_summary(task: TaskRecord) -> dict[str, str]:
    return {
        "id": task.id,
        "name": task.name,
        "status": task.status_label,
        "risk": task.overall_risk_label,
        "risk_tone": overall_risk_to_chip_tone(task.overall_risk),
        "summary": task.summary,
        "created_at": task.created_at[:19].replace("T", " "),
        "selected_profile_id": task.selected_profile_id or "",
        "selected_profile_name": task.selected_profile_name or "基础通用合同审查",
    }


def build_profile_payload(task: TaskRecord) -> dict[str, Any]:
    snapshot = task.selected_profile_snapshot or {}
    assets = snapshot.get("assets", []) if isinstance(snapshot, dict) else []
    return {
        "id": task.selected_profile_id,
        "name": task.selected_profile_name or snapshot.get("profile_name") or "基础通用合同审查",
        "version": snapshot.get("profile_version") if isinstance(snapshot, dict) else None,
        "assets": assets,
        "asset_count": len(assets),
        "hard_rule_count": sum(1 for item in assets if item.get("asset_type") == "hard_rule"),
        "semantic_rule_count": sum(1 for item in assets if item.get("asset_type") == "semantic_rule"),
        "report_template_count": sum(1 for item in assets if item.get("asset_type") == "report_template"),
    }


def build_task_decision_payload(task: TaskRecord) -> dict[str, Any]:
    task_actions = [
        action
        for action in task.review_actions
        if action.target_type == "task" and action.target_id == task.id
    ]
    latest = sorted(task_actions, key=lambda item: item.created_at, reverse=True)[0] if task_actions else None
    return {
        "status": task.status_label,
        "decision": task.decision_label,
        "latest_action": render_review_action(latest.action_type) if latest else "尚未提交整单结论",
        "latest_comment": latest.comment if latest else None,
        "latest_at": latest.created_at if latest else None,
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


def parse_clauses(text: str, rule_context: dict[str, Any] | None = None) -> list[Clause]:
    templates = (rule_context or {}).get("clause_parse_templates") or []
    for template in templates:
        clauses = parse_clauses_with_template(text, template)
        if clauses:
            return clauses
    return parse_clauses_with_fallback(text, fallback="paragraph_split")


def parse_clauses_with_template(text: str, template: dict[str, Any]) -> list[Clause]:
    pattern = template.get("header_pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return []
    try:
        header_re = re.compile(pattern, re.MULTILINE)
    except re.error:
        return []
    matches = list(header_re.finditer(text))
    if not matches:
        fallback = str(template.get("fallback") or "")
        return parse_clauses_with_fallback(
            text,
            fallback=fallback,
            parser_template_id=str(template.get("asset_id") or ""),
            parser_schema_version=str(template.get("schema_version") or ""),
        )

    clauses: list[Clause] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        clause_id = extract_match_group(match, "id") or f"C{index + 1:03d}"
        title = extract_match_group(match, "title") or match.group(0).strip()
        clauses.append(
            Clause(
                id=clause_id.strip(),
                title=title.strip(),
                text=body,
                parser_source="asset-template",
                parser_template_id=str(template.get("asset_id") or ""),
                parser_schema_version=str(template.get("schema_version") or ""),
            )
        )
    return clauses


def parse_clauses_with_fallback(
    text: str,
    *,
    fallback: str,
    parser_template_id: str | None = None,
    parser_schema_version: str | None = None,
) -> list[Clause]:
    source = f"fallback:{fallback or 'paragraph_split'}"
    if fallback == "legacy_clause_header":
        matches = list(CLAUSE_HEADER_RE.finditer(text))
        if matches:
            return [
                Clause(
                    id=(extract_match_group(match, "id") or f"C{index + 1:03d}").strip(),
                    title=(extract_match_group(match, "title") or match.group(0)).strip(),
                    text=text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip(),
                    parser_source=source,
                    parser_template_id=parser_template_id,
                    parser_schema_version=parser_schema_version,
                )
                for index, match in enumerate(matches)
            ]
    sections = [section.strip() for section in re.split(r"\n{2,}", text) if section.strip()]
    return [
        Clause(
            id=f"C{index:03d}",
            title=f"段落 {index}",
            text=section,
            parser_source=source,
            parser_template_id=parser_template_id,
            parser_schema_version=parser_schema_version,
        )
        for index, section in enumerate(sections, start=1)
    ]


def extract_match_group(match: re.Match[str], group_name: str) -> str | None:
    if group_name not in match.re.groupindex:
        return None
    value = match.group(group_name)
    return value.strip() if value else None


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
    rule_context: dict[str, Any] | None = None,
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
    apply_extraction_rules(facts, clauses, text, rule_context=rule_context)
    return facts


def apply_extraction_rules(
    facts: dict[str, dict[str, Any]],
    clauses: list[Clause],
    text: str,
    *,
    rule_context: dict[str, Any] | None = None,
) -> None:
    for asset_rule in (rule_context or {}).get("extraction_rules") or []:
        for rule in configured_extraction_rules(asset_rule):
            fact_key = first_string(rule, ["fact_key", "target_field", "field_key", "key"])
            if not fact_key:
                continue
            extracted = extract_fact_by_rule(text, clauses, rule)
            if extracted is None:
                continue
            value, evidence_ids = extracted
            set_fact(facts, fact_key, value, evidence_ids)


def apply_llm_extraction_fallback(
    facts: dict[str, dict[str, Any]],
    clauses: list[Clause],
    text: str,
    contract_type: str,
    *,
    rule_context: dict[str, Any] | None = None,
) -> None:
    prompt_template = active_prompt_template(rule_context, purpose="field_extraction")
    if not prompt_template:
        return

    missing_fields = [
        field_def
        for field_def in configured_extraction_fields(contract_type, rule_context=rule_context)
        if field_def["key"] != "contract_type" and fact_status(facts, field_def["key"]) == "missing"
    ]
    if not missing_fields:
        return

    candidates = generate_field_extraction_candidates(
        text=text,
        clauses=clauses,
        missing_fields=missing_fields,
        prompt_template=prompt_template,
    )
    for candidate in candidates:
        key = candidate.get("key")
        value = candidate.get("value")
        if not isinstance(key, str) or key not in {item["key"] for item in missing_fields}:
            continue
        if value is None or value == "":
            continue
        evidence_ids = [
            item
            for item in list_strings(candidate.get("evidence_clause_ids"))
            if any(clause.id == item for clause in clauses)
        ]
        if not evidence_ids:
            evidence_ids = evidence_ids_for_fragment(clauses, str(value))
        set_fact(
            facts,
            key,
            value,
            evidence_ids,
            status="candidate",
            metadata={
                "source": "llm-fallback",
                "prompt_template_id": prompt_template.get("asset_id"),
                "confidence": coerce_number(candidate.get("confidence")),
                "reasoning_summary": candidate.get("reasoning_summary"),
            },
        )


def active_prompt_template(rule_context: dict[str, Any] | None, *, purpose: str) -> dict[str, Any] | None:
    for prompt_template in (rule_context or {}).get("prompt_templates") or []:
        if prompt_template.get("purpose") == purpose:
            return prompt_template
    return None


def generate_field_extraction_candidates(
    *,
    text: str,
    clauses: list[Clause],
    missing_fields: list[dict[str, str]],
    prompt_template: dict[str, Any],
) -> list[dict[str, Any]]:
    settings = get_settings()
    provider = settings.llm_draft_provider
    if provider == "mock" or (provider == "auto" and not settings.llm_api_key):
        return mock_field_extraction_candidates(text=text, clauses=clauses, missing_fields=missing_fields)
    try:
        result = LLMClient(settings).complete_json(
            messages=build_field_extraction_messages(
                text=text,
                clauses=clauses,
                missing_fields=missing_fields,
                prompt_template=prompt_template,
            ),
            max_tokens=1400,
        )
    except Exception:  # noqa: BLE001
        if provider == "auto":
            return mock_field_extraction_candidates(text=text, clauses=clauses, missing_fields=missing_fields)
        return []
    payload = result.get("parsed_json")
    if not isinstance(payload, dict):
        return []
    raw_candidates = payload.get("fields") or payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    return [item for item in raw_candidates if isinstance(item, dict)]


def build_field_extraction_messages(
    *,
    text: str,
    clauses: list[Clause],
    missing_fields: list[dict[str, str]],
    prompt_template: dict[str, Any],
) -> list[dict[str, str]]:
    clause_text = "\n\n".join(f"[{clause.id}] {clause.title}\n{clause.text}" for clause in clauses[:24])
    field_text = "\n".join(f"- {field['key']}: {field['label']}" for field in missing_fields)
    return [
        {
            "role": "system",
            "content": (
                "你是合同合规审查字段抽取助手。只输出合法 JSON 对象，不输出 Markdown。"
                "输出格式为 {\"fields\":[{\"key\":\"...\",\"value\":\"...\","
                "\"confidence\":0.0,\"evidence_clause_ids\":[\"C001\"],"
                "\"reasoning_summary\":\"...\"}]}。"
                "只为用户给出的 missing 字段生成候选；没有证据时不要编造。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Prompt 模板：{prompt_template}\n"
                f"需要抽取的 missing 字段：\n{field_text}\n\n"
                f"条款：\n{clause_text}\n\n"
                f"完整合同文本：\n{text[:8000]}"
            ),
        },
    ]


def mock_field_extraction_candidates(
    *,
    text: str,
    clauses: list[Clause],
    missing_fields: list[dict[str, str]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for field_def in missing_fields:
        key = field_def["key"]
        label = field_def["label"]
        value = mock_extract_by_label(text, label)
        if value is None and "编号" in label:
            value = search_text(r"(?:例外审批编号|审批编号|项目编号)[:：]\s*([A-Z]+-\d+)", text)
        if value is None:
            continue
        candidates.append(
            {
                "key": key,
                "value": value,
                "confidence": 0.82,
                "evidence_clause_ids": evidence_ids_for_fragment(clauses, value),
                "reasoning_summary": f"mock fallback matched label {label}",
            }
        )
    return candidates


def mock_extract_by_label(text: str, label: str) -> str | None:
    if not label:
        return None
    return search_text(rf"{re.escape(label)}[:：]\s*([^\n。；;]+)", text)


def configured_extraction_rules(asset_rule: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    if first_string(asset_rule, ["fact_key", "target_field", "field_key", "key"]):
        rules.append(asset_rule)
    for key in ["rules", "extraction_rules", "extraction_targets"]:
        raw_items = asset_rule.get(key)
        if isinstance(raw_items, list):
            rules.extend(item for item in raw_items if isinstance(item, dict))
    return rules


def extract_fact_by_rule(
    text: str,
    clauses: list[Clause],
    rule: dict[str, Any],
) -> tuple[Any, list[str]] | None:
    for pattern in extraction_patterns(rule):
        try:
            match = re.search(pattern, text, re.MULTILINE)
        except re.error:
            continue
        if not match:
            continue
        value = extract_regex_value(match)
        evidence_ids = evidence_ids_for_fragment(clauses, match.group(0))
        return normalize_extracted_value(value, rule), evidence_ids

    keywords = list_strings(rule.get("keywords")) or list_strings(rule.get("keyword"))
    for keyword in keywords:
        if keyword not in text:
            continue
        evidence_ids = evidence_ids_for_fragment(clauses, keyword)
        if str(rule.get("value_type") or "").lower() == "boolean":
            return True, evidence_ids
        return str(rule.get("value") or keyword), evidence_ids
    return None


def extraction_patterns(rule: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for key in ["pattern", "regex", "regex_pattern", "extraction_pattern", "patterns"]:
        patterns.extend(list_strings(rule.get(key)))
    return patterns


def extract_regex_value(match: re.Match[str]) -> str:
    if "value" in match.re.groupindex:
        return match.group("value").strip()
    if match.lastindex:
        return match.group(1).strip()
    return match.group(0).strip()


def normalize_extracted_value(value: Any, rule: dict[str, Any]) -> Any:
    value_type = str(rule.get("value_type") or rule.get("type") or "").lower()
    text_value = str(value).strip()
    if value_type in {"percent", "percentage"}:
        number = coerce_number(text_value)
        return f"{trim_number(str(number))}%" if number is not None else text_value
    if value_type in {"amount", "money"}:
        number = coerce_number(text_value)
        return f"{trim_number(str(number))} 元" if number is not None else text_value
    if value_type == "boolean":
        if text_value.lower() in {"true", "yes", "1"} or text_value in {"是", "有", "存在"}:
            return True
        if text_value.lower() in {"false", "no", "0"} or text_value in {"否", "无", "不存在"}:
            return False
    return text_value


def evaluate_rules(
    contract_type: str,
    facts: dict[str, dict[str, Any]],
    rule_context: dict[str, Any] | None = None,
) -> list[RiskFinding]:
    rule_context = rule_context or {}
    hard_rules = rule_context.get("hard_rules") or []
    risks: list[RiskFinding] = []

    for rule in hard_rules:
        risk = evaluate_hard_rule(rule, contract_type, facts)
        if risk is not None:
            risks.append(risk)

    risks.sort(key=lambda item: risk_priority(item.level), reverse=True)
    return risks


def evaluate_semantic_rules(
    contract_type: str,
    facts: dict[str, dict[str, Any]],
    clauses: list[Clause],
    text: str,
    *,
    rule_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rule_context = rule_context or {}
    prompt_template = active_prompt_template(rule_context, purpose="semantic_rule")
    results: list[dict[str, Any]] = []
    for rule in rule_context.get("semantic_rules") or []:
        if not semantic_rule_matches_contract_type(rule, contract_type):
            continue
        result = run_semantic_rule(
            rule=rule,
            facts=facts,
            clauses=clauses,
            text=text,
            prompt_template=prompt_template,
        )
        results.append(result)
    return results


def run_semantic_rule(
    *,
    rule: dict[str, Any],
    facts: dict[str, dict[str, Any]],
    clauses: list[Clause],
    text: str,
    prompt_template: dict[str, Any] | None,
) -> dict[str, Any]:
    settings = get_settings()
    provider = settings.llm_draft_provider
    status = "success"
    error_detail: str | None = None
    if provider == "mock" or (provider == "auto" and not settings.llm_api_key):
        payload = mock_semantic_rule_result(rule=rule, facts=facts, clauses=clauses, text=text)
        provider_used = "mock"
    else:
        try:
            response = LLMClient(settings).complete_json(
                messages=build_semantic_rule_messages(
                    rule=rule,
                    facts=facts,
                    clauses=clauses,
                    text=text,
                    prompt_template=prompt_template,
                ),
                max_tokens=1200,
            )
            parsed = response.get("parsed_json")
            payload = parsed if isinstance(parsed, dict) else {}
            provider_used = "llm"
        except Exception as exc:  # noqa: BLE001
            error_detail = str(exc)
            if provider == "auto":
                payload = mock_semantic_rule_result(rule=rule, facts=facts, clauses=clauses, text=text)
                provider_used = "mock_fallback"
                status = "fallback"
            else:
                payload = {}
                provider_used = "llm"
                status = "failed"

    normalized = normalize_semantic_rule_payload(payload, clauses)
    if status != "failed":
        status = semantic_result_status(normalized)
    risk = build_semantic_risk(rule, normalized) if status == "hit" else None
    return {
        "asset_id": rule.get("asset_id"),
        "asset_version": rule.get("asset_version"),
        "schema_version": rule.get("schema_version") or rule.get("output_schema"),
        "name": rule.get("name"),
        "status": status,
        "provider": provider_used,
        "prompt_template_id": (prompt_template or {}).get("asset_id") or rule.get("prompt_template_id"),
        "confidence": normalized.get("confidence"),
        "hit": normalized.get("hit"),
        "evidence_clause_ids": normalized.get("evidence_clause_ids") or [],
        "reasoning_summary": normalized.get("reasoning_summary"),
        "error_detail": error_detail,
        "risk": risk,
    }


def build_semantic_rule_messages(
    *,
    rule: dict[str, Any],
    facts: dict[str, dict[str, Any]],
    clauses: list[Clause],
    text: str,
    prompt_template: dict[str, Any] | None,
) -> list[dict[str, str]]:
    clause_text = "\n\n".join(f"[{clause.id}] {clause.title}\n{clause.text}" for clause in clauses[:24])
    fact_payload = {
        key: {
            "value": item.get("value"),
            "status": item.get("status"),
            "evidence_clause_ids": item.get("evidence_clause_ids") or [],
        }
        for key, item in facts.items()
        if not key.startswith("_")
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a contract compliance semantic-rule reviewer. "
                "Return only a JSON object with keys: hit, confidence, evidence_clause_ids, "
                "reasoning_summary, title, risk_level, reason, action. "
                "Do not report a hit without clause evidence."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Prompt template: {prompt_template or {}}\n"
                f"Semantic rule asset: {rule}\n"
                f"Extracted facts: {fact_payload}\n\n"
                f"Clauses:\n{clause_text}\n\n"
                f"Full contract text:\n{text[:8000]}"
            ),
        },
    ]


def mock_semantic_rule_result(
    *,
    rule: dict[str, Any],
    facts: dict[str, dict[str, Any]],
    clauses: list[Clause],
    text: str,
) -> dict[str, Any]:
    renewal_clause = first_clause_matching(
        clauses,
        [
            "续约",
            "顺延",
            "续签",
            "自动续约",
            "自动顺延",
            "默认顺延",
            "ç»­çº¦",
            "é¡ºå»¶",
            "ç»­ç­¾",
        ],
    )
    renewal_text = f"{renewal_clause.title}\n{renewal_clause.text}" if renewal_clause else text
    auto_renewal = bool(fact_value(facts, "term.auto_renewal")) or any(
        keyword in renewal_text
        for keyword in ["自动续约", "自动顺延", "默认顺延", "自动续展", "è‡ªåŠ¨ç»­çº¦", "è‡ªåŠ¨é¡ºå»¶", "é»˜è®¤é¡ºå»¶"]
    )
    approval_required = bool(fact_value(facts, "approval.exception_required")) or any(
        keyword in renewal_text
        for keyword in [
            "审批",
            "书面",
            "另行签署",
            "重新签署",
            "提前通知",
            "退出",
            "å®¡æ‰¹",
            "ä¹¦é¢",
            "å¦è¡Œç­¾ç½²",
            "é‡æ–°ç­¾ç½²",
        ]
    )
    renewal_text_lower = renewal_text.lower()
    auto_renewal = auto_renewal or any(keyword in renewal_text_lower for keyword in ["auto renewal", "automatic renewal", "renew automatically"])
    approval_required = approval_required or any(keyword in renewal_text_lower for keyword in ["approval", "written renewal", "opt-out", "written confirmation"])
    hit = auto_renewal and not approval_required
    evidence_ids = ids_of([renewal_clause]) if renewal_clause else []
    if not evidence_ids and auto_renewal:
        evidence_ids = (
            evidence_ids_for_fragment(clauses, "automatic renewal")
            or evidence_ids_for_fragment(clauses, "auto renewal")
            or evidence_ids_for_fragment(clauses, "renew automatically")
        )
    return {
        "hit": hit,
        "confidence": 0.88 if hit and evidence_ids else 0.68,
        "evidence_clause_ids": evidence_ids,
        "reasoning_summary": "mock semantic check for auto-renewal approval precondition",
        "title": rule.get("title") or rule.get("name") or rule.get("asset_id"),
        "risk_level": rule.get("level") or rule.get("risk_level") or "high",
        "reason": "Semantic rule found auto-renewal language without approval or written renewal precondition.",
        "action": "Ask the business owner to add approval, written renewal, or opt-out controls.",
        "policy_reference_ids": rule.get("policy_reference_ids") or ["POLICY-REV-004"],
    }


def normalize_semantic_rule_payload(payload: dict[str, Any], clauses: list[Clause]) -> dict[str, Any]:
    hit = bool(payload.get("hit"))
    confidence = coerce_number(payload.get("confidence")) or 0.0
    valid_clause_ids = {clause.id for clause in clauses}
    evidence_ids = [
        item
        for item in list_strings(payload.get("evidence_clause_ids") or payload.get("evidence"))
        if item in valid_clause_ids
    ]
    if not evidence_ids and payload.get("evidence_text"):
        evidence_ids = evidence_ids_for_fragment(clauses, str(payload.get("evidence_text")))
    return {
        "hit": hit,
        "confidence": confidence,
        "evidence_clause_ids": evidence_ids,
        "reasoning_summary": payload.get("reasoning_summary") or payload.get("reasoning") or "",
        "title": payload.get("title"),
        "risk_level": payload.get("risk_level") or payload.get("level"),
        "reason": payload.get("reason"),
        "action": payload.get("action"),
        "policy_reference_ids": payload.get("policy_reference_ids") or payload.get("policy_reference"),
    }


def semantic_result_status(payload: dict[str, Any]) -> str:
    if not payload.get("hit"):
        return "no_hit"
    if (payload.get("confidence") or 0) < 0.75:
        return "low_confidence"
    if not payload.get("evidence_clause_ids"):
        return "missing_evidence"
    return "hit"


def build_semantic_risk(rule: dict[str, Any], payload: dict[str, Any]) -> RiskFinding:
    title = str(payload.get("title") or rule.get("title") or rule.get("name") or rule.get("asset_id"))
    return build_risk(
        rule_id=str(rule.get("rule_id") or rule.get("asset_id") or "SEMANTIC-RULE"),
        title=title,
        level=str(payload.get("risk_level") or rule.get("level") or rule.get("risk_level") or "medium"),
        reason=str(payload.get("reason") or payload.get("reasoning_summary") or "Semantic rule hit."),
        evidence_ids=list_strings(payload.get("evidence_clause_ids")),
        policy_ids=normalize_policy_ids(payload.get("policy_reference_ids") or rule.get("policy_reference_ids") or ["POLICY-REV-004"]),
        action=str(payload.get("action") or rule.get("action") or "Please review this semantic-rule finding."),
        rule_version=f"semantic:{rule.get('asset_id')}@v{rule.get('asset_version') or 1}",
    )


def semantic_rule_matches_contract_type(rule: dict[str, Any], contract_type: str) -> bool:
    applicability = rule.get("applicability")
    if isinstance(applicability, dict):
        expected = applicability.get("contract_type")
        if isinstance(expected, list):
            return contract_type in expected
        if expected:
            return expected == contract_type
    expected = rule.get("contract_type")
    if isinstance(expected, list):
        return contract_type in expected
    return not expected or expected == contract_type


def risk_is_duplicate(candidate: RiskFinding, existing: list[RiskFinding]) -> bool:
    candidate_evidence = set(candidate.evidence_clause_ids)
    for risk in existing:
        if candidate.title == risk.title:
            return True
        if candidate_evidence and candidate_evidence == set(risk.evidence_clause_ids):
            if "续约" in candidate.title or "ç»­çº¦" in candidate.title:
                return True
    return False


def evaluate_hard_rule(
    rule: dict[str, Any],
    contract_type: str,
    facts: dict[str, dict[str, Any]],
) -> RiskFinding | None:
    if not hard_rule_matches_contract_type(rule, contract_type):
        return None

    condition_tree = hard_rule_condition_tree(rule)
    if not condition_tree or not condition_tree_matches(condition_tree, facts):
        return None

    evidence_ids: list[str] = []
    evidence_fact_keys = rule.get("evidence_fact_keys") or condition_tree_fact_keys(condition_tree)
    for fact_key in evidence_fact_keys:
        evidence_ids = merge_ids(evidence_ids, fact_evidence_ids(facts, str(fact_key)))

    rule_id = str(rule.get("rule_id") or rule.get("asset_id") or "RULE-UNKNOWN")
    return build_risk(
        rule_id=rule_id,
        title=render_rule_template(str(rule.get("title") or rule_id), rule, facts),
        level=str(rule.get("level") or rule.get("risk_level") or "medium"),
        reason=render_rule_template(
            str(rule.get("reason_template") or rule.get("reason") or "\u89c4\u5219\u547d\u4e2d\u3002"),
            rule,
            facts,
        ),
        evidence_ids=evidence_ids,
        policy_ids=normalize_policy_ids(rule.get("policy_reference_ids") or rule.get("policy_reference") or []),
        action=render_rule_template(
            str(rule.get("action_template") or rule.get("action") or "\u8bf7\u4eba\u5de5\u590d\u6838\u3002"),
            rule,
            facts,
        ),
    )


def hard_rule_matches_contract_type(rule: dict[str, Any], contract_type: str) -> bool:
    applicability = rule.get("applicability")
    if isinstance(applicability, dict):
        expected = applicability.get("contract_type")
        if expected and expected != contract_type:
            return False
    expected = rule.get("contract_type")
    return not expected or expected == contract_type


def hard_rule_conditions(rule: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = rule.get("conditions")
    if isinstance(conditions, list) and conditions:
        return [condition for condition in conditions if isinstance(condition, dict)]
    fact_key = rule.get("fact_key")
    if fact_key:
        return [
            {
                "fact_key": fact_key,
                "operator": rule.get("operator") or "is",
                "value": rule.get("value"),
            }
        ]
    return []


def hard_rule_condition_tree(rule: dict[str, Any]) -> dict[str, Any] | None:
    for key in ["condition_tree", "condition", "where"]:
        value = rule.get(key)
        if isinstance(value, dict) and value:
            return value
    conditions = hard_rule_conditions(rule)
    if conditions:
        return {"all": conditions}
    return None


def condition_tree_matches(node: Any, facts: dict[str, dict[str, Any]]) -> bool:
    if isinstance(node, list):
        return all(condition_tree_matches(item, facts) for item in node)
    if not isinstance(node, dict) or not node:
        return False
    if "all" in node:
        children = node.get("all")
        return isinstance(children, list) and bool(children) and all(
            condition_tree_matches(child, facts) for child in children
        )
    if "any" in node:
        children = node.get("any")
        return isinstance(children, list) and bool(children) and any(
            condition_tree_matches(child, facts) for child in children
        )
    if "not" in node:
        return not condition_tree_matches(node.get("not"), facts)
    return condition_matches(node, facts)


def condition_tree_fact_keys(node: Any) -> list[str]:
    if isinstance(node, list):
        keys: list[str] = []
        for item in node:
            keys = merge_ids(keys, condition_tree_fact_keys(item))
        return keys
    if not isinstance(node, dict):
        return []
    keys: list[str] = []
    for branch_key in ["all", "any"]:
        if branch_key in node:
            return condition_tree_fact_keys(node.get(branch_key))
    if "not" in node:
        return condition_tree_fact_keys(node.get("not"))
    fact_key = node.get("fact_key")
    if fact_key:
        keys.append(str(fact_key))
    expected_fact_key = node.get("expected_fact_key")
    if expected_fact_key:
        keys.append(str(expected_fact_key))
    return keys


def condition_matches(condition: dict[str, Any], facts: dict[str, dict[str, Any]]) -> bool:
    fact_key = str(condition.get("fact_key") or "")
    operator = normalize_operator(condition.get("operator") or "is")
    expected = condition_expected_value(condition, facts)
    actual = fact_value(facts, fact_key)
    status = fact_status(facts, fact_key)

    if operator == "missing":
        return status == "missing" or actual is None or actual == ""
    if operator == "present":
        return status != "missing" and actual is not None and actual != ""
    if operator in {">", ">=", "<", "<=", "==", "!=", "between"}:
        actual_number = coerce_number(actual)
        if actual_number is None:
            return False
        if operator == "between":
            bounds = expected if isinstance(expected, list) else condition.get("range")
            if not isinstance(bounds, list) or len(bounds) != 2:
                return False
            lower = coerce_number(bounds[0])
            upper = coerce_number(bounds[1])
            if lower is None or upper is None:
                return False
            return lower <= actual_number <= upper
        expected_number = coerce_number(expected)
        if expected_number is None:
            return False
        return compare_number(actual_number, expected_number, operator)
    if operator == "is":
        return normalize_rule_value(actual) == normalize_rule_value(expected)
    if operator == "is_not":
        return normalize_rule_value(actual) != normalize_rule_value(expected)
    if operator == "contains":
        return expected is not None and str(expected) in str(actual or "")
    if operator == "not_contains":
        return expected is not None and str(expected) not in str(actual or "")
    if operator == "contains_any":
        return any(str(item) in str(actual or "") for item in list_strings(expected))
    if operator == "contains_all":
        expected_items = list_strings(expected)
        return bool(expected_items) and all(str(item) in str(actual or "") for item in expected_items)
    if operator == "in":
        expected_items = [normalize_rule_value(item) for item in list_strings(expected)]
        return normalize_rule_value(actual) in expected_items
    if operator == "not_in":
        expected_items = [normalize_rule_value(item) for item in list_strings(expected)]
        return normalize_rule_value(actual) not in expected_items
    if operator == "matches_regex":
        return isinstance(expected, str) and re.search(expected, str(actual or "")) is not None
    return False


def normalize_operator(value: Any) -> str:
    operator = str(value or "is").strip().lower()
    aliases = {
        "=": "==",
        "equals": "is",
        "eq": "is",
        "not_equals": "is_not",
        "neq": "is_not",
        "not in": "not_in",
        "regex": "matches_regex",
    }
    return aliases.get(operator, operator)


def condition_expected_value(condition: dict[str, Any], facts: dict[str, dict[str, Any]]) -> Any:
    expected_fact_key = condition.get("expected_fact_key")
    if expected_fact_key:
        return fact_value(facts, str(expected_fact_key))
    return condition.get("value")


def coerce_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    return float(match.group(0))


def compare_number(actual: float, expected: float, operator: str) -> bool:
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    return False


def normalize_rule_value(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
        return value.strip()
    return value


def render_rule_template(template: str, rule: dict[str, Any], facts: dict[str, dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "asset_id":
            return str(rule.get("asset_id") or "")
        if key == "threshold":
            return rule_threshold_text(rule)
        if key in facts:
            value = fact_value(facts, key)
            return format_field_value(value)
        if key in rule:
            return str(rule[key])
        return match.group(0)

    return re.sub(r"\{([^{}]+)\}", replace, template)


def rule_threshold_text(rule: dict[str, Any]) -> str:
    for condition in hard_rule_leaf_conditions(rule):
        if condition.get("fact_key") == "payment.prepay_ratio" and condition.get("value") is not None:
            number = coerce_number(condition.get("value"))
            return trim_number(str(number)) if number is not None else str(condition.get("value"))
    number = coerce_number(rule.get("value"))
    return trim_number(str(number)) if number is not None else ""


def hard_rule_leaf_conditions(rule: dict[str, Any]) -> list[dict[str, Any]]:
    condition_tree = hard_rule_condition_tree(rule)
    if condition_tree:
        return condition_tree_leaf_conditions(condition_tree)
    return hard_rule_conditions(rule)


def condition_tree_leaf_conditions(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        items: list[dict[str, Any]] = []
        for child in node:
            items.extend(condition_tree_leaf_conditions(child))
        return items
    if not isinstance(node, dict):
        return []
    items: list[dict[str, Any]] = []
    for branch_key in ["all", "any"]:
        if branch_key in node:
            return condition_tree_leaf_conditions(node.get(branch_key))
    if "not" in node:
        return condition_tree_leaf_conditions(node.get("not"))
    if node.get("fact_key"):
        items.append(node)
    return items


def normalize_policy_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def build_risk(
    rule_id: str,
    title: str,
    level: str,
    reason: str,
    evidence_ids: list[str],
    policy_ids: list[str],
    action: str,
    rule_version: str = "mvp-rules-v1",
) -> RiskFinding:
    policy_reference_ids = merge_ids(policy_ids, ["POLICY-REV-003"])
    return RiskFinding(
        rule_id=rule_id,
        title=title,
        level=level,
        message=title,
        reason=reason,
        evidence_clause_ids=evidence_ids,
        policy_reference_ids=policy_reference_ids,
        action=action,
        rule_version=rule_version,
    )


def active_risk_message_template(rule_context: dict[str, Any] | None) -> dict[str, Any] | None:
    templates = (rule_context or {}).get("risk_message_templates") or []
    return templates[0] if templates else None


def apply_risk_message_templates(
    risks: list[RiskFinding],
    *,
    rule_context: dict[str, Any] | None = None,
) -> list[RiskFinding]:
    template = active_risk_message_template(rule_context)
    if not template:
        return risks
    raw_template = str(template.get("template") or "{rule_title}: {reason} 建议：{action}")
    return [
        risk.model_copy(update={"message": render_risk_message_template(raw_template, risk)})
        for risk in risks
    ]


def render_risk_message_template(template: str, risk: RiskFinding) -> str:
    values = {
        "rule_title": risk.title,
        "title": risk.title,
        "reason": risk.reason,
        "action": risk.action,
        "risk_level": risk.level,
        "level": risk.level,
        "rule_id": risk.rule_id,
        "policy_reference_ids": "、".join(risk.policy_reference_ids),
        "evidence_clause_ids": "、".join(risk.evidence_clause_ids) or "未定位",
    }

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return str(values.get(key, match.group(0)))

    return re.sub(r"\{([^{}]+)\}", replace, template)


def build_extracted_fields(
    contract_type: str,
    facts: dict[str, dict[str, Any]],
    rule_context: dict[str, Any] | None = None,
) -> list[ExtractedField]:
    fields: list[ExtractedField] = []
    field_defs = configured_extraction_fields(contract_type, rule_context=rule_context)
    for field_def in field_defs:
        key = field_def["key"]
        value = fact_value(facts, key)
        if key == "contract_type":
            value = CONTRACT_TYPE_LABELS.get(contract_type, "未识别合同")
        fields.append(
            ExtractedField(
                key=key,
                label=field_def["label"],
                value=format_field_value(value),
                status=fact_status(facts, key),
                evidence_clause_ids=fact_evidence_ids(facts, key),
            )
        )
    return fields


def configured_extraction_fields(
    contract_type: str,
    *,
    rule_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    schemas = (rule_context or {}).get("extraction_schemas") or []
    schema = schemas[-1] if schemas else {}
    raw_fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(raw_fields, list) or not raw_fields:
        return [{"key": key, "label": FIELD_LABELS.get(key, key)} for key in DISPLAY_FIELD_ORDER]

    label_overrides = extraction_label_overrides(rule_context)
    fields: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_field in raw_fields:
        key: str | None = None
        label: str | None = None
        if isinstance(raw_field, str):
            key = raw_field
        elif isinstance(raw_field, dict):
            key = first_string(raw_field, ["key", "fact_key", "field_key", "target_field", "name"])
            label = first_string(raw_field, ["label", "title", "display_name"])
        if not key or key in seen:
            continue
        seen.add(key)
        fields.append({"key": key, "label": label or label_overrides.get(key) or FIELD_LABELS.get(key, key)})

    if not fields:
        return [{"key": key, "label": FIELD_LABELS.get(key, key)} for key in DISPLAY_FIELD_ORDER]
    if "contract_type" not in seen:
        fields.insert(0, {"key": "contract_type", "label": FIELD_LABELS["contract_type"]})
    return fields


def extraction_label_overrides(rule_context: dict[str, Any] | None = None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for asset_rule in (rule_context or {}).get("extraction_rules") or []:
        for rule in configured_extraction_rules(asset_rule):
            key = first_string(rule, ["fact_key", "target_field", "field_key", "key"])
            label = first_string(rule, ["label", "title", "display_name"])
            if key and label:
                labels[key] = label
    return labels


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


def derive_overall_risk(risks: list[RiskFinding], rule_context: dict[str, Any] | None = None) -> str:
    policy = active_risk_policy(rule_context)
    if policy is not None:
        if any(risk.level == "high" for risk in risks):
            return str((policy.get("high") or {}).get("overall_risk") or "red")
        if any(risk.level == "medium" for risk in risks):
            return str((policy.get("medium") or {}).get("overall_risk") or "yellow")
        return str((policy.get("none") or {}).get("overall_risk") or "green")
    if any(risk.level == "high" for risk in risks):
        return "red"
    if any(risk.level == "medium" for risk in risks):
        return "yellow"
    return "green"


def derive_status(
    overall_risk: str,
    risks: list[RiskFinding] | None = None,
    rule_context: dict[str, Any] | None = None,
) -> str:
    if risks is not None and risks and all(is_review_resolved(risk) for risk in risks):
        return "review_completed"
    policy = active_risk_policy(rule_context)
    if policy is not None:
        if overall_risk == "red":
            return str((policy.get("high") or {}).get("status") or "pending_review")
        if overall_risk == "yellow":
            return str((policy.get("medium") or {}).get("status") or "watchlist")
        return str((policy.get("none") or {}).get("status") or "ready")
    if overall_risk == "red":
        return "pending_review"
    if overall_risk == "yellow":
        return "watchlist"
    return "ready"


def active_risk_policy(rule_context: dict[str, Any] | None) -> dict[str, Any] | None:
    policies = (rule_context or {}).get("risk_evaluation_policies") or []
    if not policies:
        return None
    content = policies[-1]
    return content if isinstance(content, dict) else None


def active_report_template(rule_context: dict[str, Any] | None) -> dict[str, Any] | None:
    templates = (rule_context or {}).get("report_templates") or []
    template = templates[-1] if templates else None
    return template if isinstance(template, dict) else None


def rule_context_from_profile_snapshot(snapshot: dict | None) -> dict[str, Any]:
    assets = (snapshot or {}).get("assets") if isinstance(snapshot, dict) else []
    context: dict[str, Any] = {
        "hard_rules": [],
        "clause_parse_templates": [],
        "extraction_schemas": [],
        "extraction_rules": [],
        "prompt_templates": [],
        "semantic_rules": [],
        "policy_references": [],
        "risk_message_templates": [],
        "report_templates": [],
        "risk_evaluation_policies": [],
    }
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict):
            continue
        asset_type = asset.get("asset_type")
        content = asset.get("content") if isinstance(asset.get("content"), dict) else {}
        item = {
            "asset_id": asset.get("asset_id"),
            "asset_version": asset.get("version"),
            "asset_content_hash": asset.get("content_hash"),
            "schema_version": asset.get("schema_version"),
            "name": asset.get("name"),
            **content,
        }
        if asset_type == "risk_message_template":
            context["risk_message_templates"].append(item)
        if asset_type == "report_template":
            context["report_templates"].append(item)
        if asset_type == "risk_evaluation_policy":
            context["risk_evaluation_policies"].append(item)
        if asset_type == "hard_rule":
            context["hard_rules"].append(item)
            _restore_legacy_threshold_context(context, item)
        if asset_type == "clause_parse_template":
            context["clause_parse_templates"].append(item)
        if asset_type == "extraction_schema":
            context["extraction_schemas"].append(item)
        if asset_type == "extraction_rule":
            context["extraction_rules"].append(item)
        if asset_type == "prompt_template":
            context["prompt_templates"].append(item)
        if asset_type == "semantic_rule":
            context["semantic_rules"].append(item)
        if asset_type == "policy_reference":
            context["policy_references"].append(item)
    return context


def _restore_legacy_threshold_context(context: dict[str, Any], hard_rule: dict[str, Any]) -> None:
    if hard_rule.get("rule_id") != "prepay_ratio_gt_threshold":
        return
    condition = hard_rule.get("condition") if isinstance(hard_rule.get("condition"), dict) else {}
    threshold = condition.get("threshold")
    if isinstance(threshold, int | float):
        context["prepay_threshold"] = threshold
        context["prepay_threshold_asset_id"] = hard_rule.get("asset_id")


def derive_decision(overall_risk: str, risks: list[RiskFinding] | None = None) -> str:
    if risks is not None and risks and all(is_review_resolved(risk) for risk in risks):
        if overall_risk == "red":
            return "revision_required"
        if overall_risk == "yellow":
            return "conditional_pass"
    if overall_risk == "red":
        return "manual_review"
    if overall_risk == "yellow":
        return "review_recommended"
    return "pass"


def is_review_resolved(risk: RiskFinding) -> bool:
    return risk.review_status in {"confirmed", "rejected", "revised"}


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


def list_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    return []


def first_string(payload: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def evidence_ids_for_fragment(clauses: list[Clause], fragment: str) -> list[str]:
    if not fragment:
        return []
    for clause in clauses:
        if fragment in clause.text or fragment in clause.title:
            return [clause.id]
    return []


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
    *,
    status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    resolved_status = status or ("missing" if value is None or value == "" else "present")
    facts[key] = {
        "value": value,
        "status": resolved_status,
        "evidence_clause_ids": evidence_clause_ids or [],
        "metadata": metadata or {},
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


def risk_source(risk: RiskFinding) -> str:
    return "semantic_rule" if risk.rule_version.startswith("semantic:") else "hard_rule"


def risk_source_label(risk: RiskFinding) -> str:
    return "LLM semantic rule" if risk_source(risk) == "semantic_rule" else "hard rule"


def overall_risk_to_chip_tone(overall_risk: str) -> str:
    return {"red": "high", "yellow": "medium", "green": "low"}[overall_risk]


def overall_risk_to_state_class(overall_risk: str) -> str:
    return {"red": "state-danger", "yellow": "state-warn", "green": "state-ok"}[overall_risk]


def render_policy_reference(policy_id: str, policy_titles: dict[str, str] | None = None) -> str:
    title = (policy_titles or {}).get(policy_id) or POLICY_TITLES.get(policy_id)
    if title:
        return f"{policy_id} {title}"
    return policy_id


def policy_reference_titles_from_snapshot(snapshot: dict | None) -> dict[str, str]:
    titles: dict[str, str] = {}
    if not isinstance(snapshot, dict):
        return titles
    for asset in snapshot.get("assets") or []:
        if not isinstance(asset, dict) or asset.get("asset_type") != "policy_reference":
            continue
        content = asset.get("content") if isinstance(asset.get("content"), dict) else {}
        reference_id = content.get("reference_id") or content.get("policy_id") or asset.get("asset_id")
        title = content.get("title") or asset.get("name")
        if reference_id and title:
            titles[str(reference_id)] = str(title)
    return titles


def render_review_status(status: str) -> str:
    labels = {
        "pending": "待复核",
        "confirmed": "已确认",
        "rejected": "已驳回",
        "revised": "已改写",
        "evidence_requested": "待补证据",
    }
    return labels.get(status, status)


def render_review_action(action_type: str) -> str:
    labels = {
        "confirm": "确认风险",
        "reject": "驳回命中",
        "rewrite_suggestion": "改写建议",
        "request_evidence": "要求补证据",
        "approve": "提交通过",
        "return_materials": "退回补材料",
        "require_revision": "要求整改",
        "archive": "归档",
        "generate_delivery_report": "生成交付报告",
    }
    return labels.get(action_type, action_type)


def build_workflow_steps(
    created_at: str,
    status: str,
    *,
    clauses: list[Clause] | None = None,
    semantic_results: list[dict[str, Any]] | None = None,
) -> list[WorkflowStep]:
    review_status = "waiting" if status == "pending_review" else "done"
    parser_sources = {clause.parser_source for clause in clauses or []}
    parsing_status = "warning" if any(source.startswith("fallback:") for source in parser_sources) else "done"
    semantic_results = semantic_results or []
    semantic_warning_statuses = {"failed", "fallback", "low_confidence", "missing_evidence"}
    semantic_status = (
        "warning"
        if any(result.get("status") in semantic_warning_statuses for result in semantic_results)
        else "done"
    )
    return [
        WorkflowStep(key="uploaded", label="原件存档", status="done", updated_at=created_at),
        WorkflowStep(key="parsing", label="条款解析", status=parsing_status, updated_at=created_at),
        WorkflowStep(key="extracting", label="事实抽取", status="done", updated_at=created_at),
        WorkflowStep(key="evaluating", label="规则裁决", status="done", updated_at=created_at),
        WorkflowStep(key="semantic_rules", label="LLM semantic rules", status=semantic_status, updated_at=created_at),
        WorkflowStep(key="review", label="人工复核", status=review_status, updated_at=created_at),
        WorkflowStep(key="report", label="报告快照", status="done", updated_at=created_at),
    ]


def build_semantic_trace_events(created_at: str, semantic_results: list[dict[str, Any]]) -> list[AgentTraceEvent]:
    if not semantic_results:
        return []
    hit_count = sum(1 for result in semantic_results if result.get("status") == "hit")
    warning_count = sum(
        1
        for result in semantic_results
        if result.get("status") in {"failed", "fallback", "low_confidence", "missing_evidence"}
    )
    return [
        AgentTraceEvent(
            at=created_at,
            type="semantic.evaluate",
            message=f"Semantic rules completed: {hit_count} hit, {warning_count} warning.",
            payload={
                "semantic_rule_count": len(semantic_results),
                "hit_count": hit_count,
                "warning_count": warning_count,
                "results": [
                    {
                        "asset_id": result.get("asset_id"),
                        "status": result.get("status"),
                        "provider": result.get("provider"),
                        "confidence": result.get("confidence"),
                        "evidence_clause_ids": result.get("evidence_clause_ids") or [],
                        "prompt_template_id": result.get("prompt_template_id"),
                    }
                    for result in semantic_results
                ],
            },
        )
    ]


def build_agent_trace(
    created_at: str,
    contract_type: str,
    clauses: list[Clause],
    extracted_fields: list[ExtractedField],
    risks: list[RiskFinding],
    status: str,
    semantic_results: list[dict[str, Any]] | None = None,
) -> list[AgentTraceEvent]:
    awaiting_review = status == "pending_review"
    llm_candidate_fields = [field.key for field in extracted_fields if field.status == "candidate"]
    semantic_results = semantic_results or []
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
            payload={
                "clause_count": len(clauses),
                "parser_source": clauses[0].parser_source if clauses else "none",
                "parser_template_id": clauses[0].parser_template_id if clauses else None,
                "parser_schema_version": clauses[0].parser_schema_version if clauses else None,
                "fallback_used": bool(clauses and clauses[0].parser_source.startswith("fallback:")),
            },
        ),
        AgentTraceEvent(
            at=created_at,
            type="fact.extract",
            message=f"完成关键事实抽取，当前输出 {len(extracted_fields)} 个字段。",
            payload={
                "field_count": len(extracted_fields),
                "llm_candidate_count": len(llm_candidate_fields),
                "llm_candidate_fields": llm_candidate_fields,
            },
        ),
        AgentTraceEvent(
            at=created_at,
            type="rule.evaluate",
            message=f"规则引擎完成初筛，命中 {len(risks)} 条风险。",
            payload={"risk_count": len(risks)},
        ),
        *build_semantic_trace_events(created_at, semantic_results),
        AgentTraceEvent(
            at=created_at,
            type="review.route",
            message="由于存在高/中风险，任务已进入人工复核队列。"
            if awaiting_review
            else "当前任务未命中高风险，已可直接进入报告确认。",
            payload={"status": status},
        ),
    ]


def build_report_snapshot(
    name: str,
    risks: list[RiskFinding],
    decision: str,
    report_type: str = "process_snapshot",
    generated_by: str = "system",
    rule_context: dict[str, Any] | None = None,
) -> ReportSnapshot:
    high_count = sum(risk.level == "high" for risk in risks)
    medium_count = sum(risk.level == "medium" for risk in risks)
    template = active_report_template(rule_context)
    sections = build_report_sections(risks, decision, template)
    report_type_label = "交付报告" if report_type == "delivery_report" else "过程快照"
    title_suffix = "审查交付报告" if report_type == "delivery_report" else "审查报告快照"
    return ReportSnapshot(
        title=f"{name} {title_suffix}",
        summary=f"系统识别 {len(risks)} 条风险，其中高风险 {high_count} 条、中风险 {medium_count} 条。",
        recommendation=build_report_recommendation(risks, decision),
        sections=sections,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        report_type=report_type,
        report_type_label=report_type_label,
        generated_by=generated_by,
    )


DEFAULT_REPORT_SECTIONS = ["current_status", "summary", "recommendation", "rule_hits", "review_actions"]
REPORT_SECTION_LABELS = {
    "current_status": "当前状态",
    "summary": "审查摘要",
    "recommendation": "建议后继操作",
    "rule_hits": "风险及规则命中",
    "review_actions": "人工复核记录",
    "fields": "结构化字段",
    "clauses": "条款结构说明",
    "policy_references": "制度依据",
    "workflow": "执行过程留痕",
}


def build_report_sections(
    risks: list[RiskFinding],
    decision: str,
    template: dict[str, Any] | None,
) -> list[dict[str, str]]:
    raw_sections = template.get("sections") if template else None
    section_keys = [str(item) for item in raw_sections] if isinstance(raw_sections, list) and raw_sections else DEFAULT_REPORT_SECTIONS
    template_name = str(template.get("name")) if template and template.get("name") else "默认报告模板"
    return [
        {
            "key": key,
            "label": REPORT_SECTION_LABELS.get(key, key),
            "summary": render_report_section_summary(key, risks, decision),
            "template": template_name,
        }
        for key in section_keys
    ]


def render_report_section_summary(key: str, risks: list[RiskFinding], decision: str) -> str:
    high_count = sum(risk.level == "high" for risk in risks)
    medium_count = sum(risk.level == "medium" for risk in risks)
    if key == "current_status":
        return f"当前结论：{DECISION_LABELS.get(decision, decision)}。"
    if key == "summary":
        return f"识别风险 {len(risks)} 条，其中高风险 {high_count} 条、中风险 {medium_count} 条。"
    if key == "recommendation":
        return build_report_recommendation(risks, decision)
    if key == "rule_hits":
        return "；".join(f"{risk.rule_id} {risk.title}" for risk in risks[:5]) or "未命中风险规则。"
    if key == "review_actions":
        return "人工复核动作将在交付报告生成后持续补充。"
    if key == "fields":
        return "结构化字段按当前配置集的 extraction_schema 和 extraction_rule 输出。"
    if key == "clauses":
        return "条款结构按当前配置集的 clause_parse_template 输出。"
    if key == "policy_references":
        policy_ids = sorted({policy_id for risk in risks for policy_id in risk.policy_reference_ids})
        return "、".join(policy_ids) if policy_ids else "未引用制度依据。"
    if key == "workflow":
        return "执行过程以 WorkflowRun/StepRun 和 Agent trace 留痕。"
    return "该章节由 report_template 定义，当前暂无专用渲染器。"


def build_report_recommendation(risks: list[RiskFinding], decision: str) -> str:
    if not risks:
        return "建议完成人工抽样复核后进入签署或执行流程。"
    if decision == "final_approved":
        return "人工复核已提交通过；如仍存在风险提示，应在归档材料中保留例外审批或处理说明。"
    if decision == "returned_for_materials":
        return "任务已退回补充材料；建议业务侧补齐审批、证明或说明后重新提交审查。"
    if decision == "archived":
        return "审查任务已归档；当前报告可作为审计留痕和后续复盘依据。"
    if decision == "manual_review":
        return "建议审查人优先处理高风险项，补齐依据材料后再确认是否放行。"
    if decision == "revision_required":
        return "人工复核已完成，仍存在有效高风险项，建议完成条款整改或例外审批后再进入签署流程。"
    if decision == "review_recommended":
        return "建议结合业务背景复核中风险项，必要时补充条款和审批依据。"
    if decision == "conditional_pass":
        return "人工复核已完成，当前仍存在中风险提示，建议带条件通过并在归档材料中保留处理说明。"
    return "当前审查结果可作为后续报告与归档的基础版本。"
