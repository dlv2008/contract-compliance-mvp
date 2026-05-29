from __future__ import annotations


def build_dashboard() -> dict:
    return {
        "headline": {
            "title": "财务合规审查工作台",
            "subtitle": "围绕合同、制度依据、风险规则和人工复核组织一个完整的审查闭环。",
            "badges": ["合同范围已锁定", "规则裁决优先", "移动端可展示"],
        },
        "metrics": [
            {"label": "样本合同", "value": "5", "tone": "neutral"},
            {"label": "规则条目", "value": "15", "tone": "warn"},
            {"label": "关键字段", "value": "24", "tone": "ok"},
            {"label": "待复核任务", "value": "2", "tone": "danger"},
        ],
        "tasks": [
            {
                "id": "demo-001",
                "name": "采购合同-样本B",
                "owner": "财务合规",
                "status": "待复核",
                "risk": "高",
                "summary": "收款账户与签约主体不一致，缺少明确变更说明。",
            },
            {
                "id": "demo-002",
                "name": "服务合同-样本B",
                "owner": "财务合规",
                "status": "已生成报告",
                "risk": "中",
                "summary": "预付款比例偏高，验收前付款约束不足。",
            },
        ],
        "timeline": [
            "上传合同并生成任务",
            "解析条款并抽取关键字段",
            "检索制度依据和历史规则",
            "输出风险卡片和证据定位",
            "人工复核后导出报告",
        ],
    }


def build_review() -> dict:
    return {
        "task": {
            "id": "demo-001",
            "name": "采购合同-样本B-收款账户不一致风险版",
            "status": "待复核",
            "risk": "高风险",
            "contract_type": "采购合同",
        },
        "summary_cards": [
            {"label": "总风险数", "value": "3"},
            {"label": "高风险", "value": "1"},
            {"label": "命中规则", "value": "R-ACC-002"},
            {"label": "人工状态", "value": "未复核"},
        ],
        "clauses": [
            {"id": "C001", "title": "合同主体", "status": "ok"},
            {"id": "C002", "title": "付款条件", "status": "warn"},
            {"id": "C003", "title": "收款账户", "status": "danger"},
            {"id": "C004", "title": "发票条款", "status": "warn"},
            {"id": "C005", "title": "违约责任", "status": "ok"},
        ],
        "contract_excerpt": [
            {
                "id": "C001",
                "title": "合同主体",
                "text": "甲方为星航电力有限公司，乙方为南宁智联设备有限公司。",
            },
            {
                "id": "C002",
                "title": "付款条件",
                "text": "甲方应在合同签署后 5 个工作日内支付合同总额的 60% 作为预付款。",
            },
            {
                "id": "C003",
                "title": "收款账户",
                "text": "乙方指定收款账户开户名为广西某科技服务有限公司，账号略。",
            },
            {
                "id": "C004",
                "title": "发票条款",
                "text": "乙方应在甲方付款后开具合法有效的增值税专用发票。",
            },
        ],
        "risks": [
            {
                "level": "high",
                "title": "收款账户与签约主体不一致",
                "rule": "R-ACC-002",
                "reason": "合同签约主体为南宁智联设备有限公司，但收款账户开户名为广西某科技服务有限公司。",
                "evidence": "C003",
                "policy": "财务付款管理制度-MVP版 3.2",
                "action": "要求补充账户变更证明或改为签约主体同名账户。",
            },
            {
                "level": "medium",
                "title": "预付款比例偏高",
                "rule": "R-PAY-001",
                "reason": "合同约定签约后支付 60% 预付款，超过 MVP 规则阈值。",
                "evidence": "C002",
                "policy": "财务付款管理制度-MVP版 4.1",
                "action": "建议加入阶段验收节点，并将预付款比例降至 30% 以内。",
            },
            {
                "level": "medium",
                "title": "发票开具时点不合理",
                "rule": "R-INV-001",
                "reason": "约定为付款后开票，不利于付款审批闭环。",
                "evidence": "C004",
                "policy": "合同审查操作指引-MVP版 5.3",
                "action": "建议改为付款前提供合法有效发票或明确先票后款条件。",
            },
        ],
        "fields": [
            {"label": "合同金额", "value": "980,000 元"},
            {"label": "预付款比例", "value": "60%"},
            {"label": "收款账户主体", "value": "广西某科技服务有限公司"},
            {"label": "发票类型", "value": "增值税专用发票"},
        ],
    }
