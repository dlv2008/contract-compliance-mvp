from __future__ import annotations

from app.models import ConfigAsset, ReviewProfile, ReviewProfileAssetRef


BASIC_PROFILE_ID = "profile-basic-contract-review-v1"
PROCUREMENT_BASIC_PROFILE_ID = "profile-procurement-basic-v1"
PROCUREMENT_ADVANCED_PROFILE_ID = "profile-procurement-advanced-v1"
SERVICE_BASIC_PROFILE_ID = "profile-service-basic-v1"


class AssetNotFoundError(ValueError):
    pass


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
    ),
    _asset(
        "asset-hardrule-prepay-v1",
        "hard_rule",
        "预付款比例控制",
        applicability={"contract_type": ["procurement_contract", "service_contract"]},
        content={
            "fact_key": "payment.prepay_ratio",
            "operator": ">",
            "value": 30,
            "risk_level": "high",
        },
        schema_version="hard-rule-v1",
    ),
    _asset(
        "asset-hardrule-payee-v1",
        "hard_rule",
        "收款主体一致性控制",
        applicability={"contract_type": ["procurement_contract", "service_contract"]},
        content={
            "fact_key": "account.same_as_counterparty",
            "operator": "is",
            "value": False,
            "risk_level": "high",
        },
        schema_version="hard-rule-v1",
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
    ),
    _asset(
        "asset-risk-message-basic-v1",
        "risk_message_template",
        "基础风险提示模板",
        content={
            "template": "{rule_title}: {reason} 建议：{action}",
        },
        schema_version="risk-message-template-v1",
    ),
    _asset(
        "asset-report-compliance-basic-v1",
        "report_template",
        "合规审查交付报告",
        content={
            "sections": ["current_status", "summary", "recommendation", "rule_hits", "review_actions"],
        },
        schema_version="report-template-v1",
    ),
    _asset(
        "asset-prompt-rule-draft-v1",
        "prompt_template",
        "制度文档生成规则草稿 Prompt",
        content={
            "purpose": "rule_draft",
            "output_schema": "rule-draft-v1",
        },
        schema_version="prompt-template-v1",
    ),
    _asset(
        "asset-prompt-semantic-rule-v1",
        "prompt_template",
        "语义规则结构化判断 Prompt",
        content={
            "purpose": "semantic_rule",
            "output_schema": "semantic-rule-result-v1",
        },
        schema_version="prompt-template-v1",
    ),
]


def _ref(asset_id: str) -> ReviewProfileAssetRef:
    asset = next(item for item in SEED_ASSETS if item.id == asset_id)
    return ReviewProfileAssetRef(
        asset_id=asset.id,
        asset_type=asset.asset_type,
        asset_version=asset.version,
    )


SEED_PROFILES: list[ReviewProfile] = [
    ReviewProfile(
        id=BASIC_PROFILE_ID,
        name="基础通用合同审查",
        version=1,
        status="active",
        applicability={"contract_type": "unknown_contract"},
        description="用于旧任务迁移和最小演示的通用配置集。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
    ReviewProfile(
        id=PROCUREMENT_BASIC_PROFILE_ID,
        name="采购合同基础审查",
        version=1,
        status="active",
        applicability={"contract_type": "procurement_contract"},
        description="采购合同基础配置集，覆盖条款解析、采购字段、核心硬规则和基础报告模板。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-extraction-procurement-v1"),
            _ref("asset-hardrule-prepay-v1"),
            _ref("asset-hardrule-payee-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
    ReviewProfile(
        id=PROCUREMENT_ADVANCED_PROFILE_ID,
        name="采购合同升级审查",
        version=1,
        status="active",
        applicability={"contract_type": "procurement_contract", "profile_level": "advanced"},
        description="采购合同升级配置集，包含语义规则示例和 Prompt 模板。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-extraction-procurement-v1"),
            _ref("asset-hardrule-prepay-v1"),
            _ref("asset-hardrule-payee-v1"),
            _ref("asset-semantic-auto-renewal-v1"),
            _ref("asset-prompt-semantic-rule-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
    ReviewProfile(
        id=SERVICE_BASIC_PROFILE_ID,
        name="服务合同基础审查",
        version=1,
        status="active",
        applicability={"contract_type": "service_contract"},
        description="服务合同基础配置集，覆盖服务合同字段、预付款、续约和责任条款关注点。",
        assets=[
            _ref("asset-clause-standard-cn-v1"),
            _ref("asset-extraction-service-v1"),
            _ref("asset-hardrule-prepay-v1"),
            _ref("asset-semantic-auto-renewal-v1"),
            _ref("asset-risk-policy-basic-v1"),
            _ref("asset-risk-message-basic-v1"),
            _ref("asset-report-compliance-basic-v1"),
        ],
    ),
]


class AssetRegistry:
    def list_assets(
        self,
        *,
        asset_type: str | None = None,
        status: str | None = None,
    ) -> list[ConfigAsset]:
        items = SEED_ASSETS
        if asset_type:
            items = [item for item in items if item.asset_type == asset_type]
        if status:
            items = [item for item in items if item.status == status]
        return items

    def list_profiles(
        self,
        *,
        status: str | None = "active",
        contract_type: str | None = None,
    ) -> list[ReviewProfile]:
        items = SEED_PROFILES
        if status:
            items = [item for item in items if item.status == status]
        if contract_type:
            items = [
                item
                for item in items
                if item.applicability.get("contract_type") in {contract_type, "unknown_contract"}
            ]
        return items

    def get_active_profile(self, profile_id: str | None) -> ReviewProfile:
        if not profile_id:
            raise AssetNotFoundError("请选择审查配置集。")
        profile = next((item for item in SEED_PROFILES if item.id == profile_id), None)
        if profile is None or profile.status != "active":
            raise AssetNotFoundError("审查配置集不存在或未生效。")
        return profile

    def freeze_profile(self, profile: ReviewProfile) -> dict:
        assets = []
        for ref in profile.assets:
            asset = next((item for item in SEED_ASSETS if item.id == ref.asset_id), None)
            if asset is None:
                continue
            assets.append(
                {
                    "asset_id": asset.id,
                    "asset_type": asset.asset_type,
                    "name": asset.name,
                    "version": asset.version,
                    "schema_version": asset.schema_version,
                }
            )
        return {
            "profile_id": profile.id,
            "profile_name": profile.name,
            "profile_version": profile.version,
            "status": profile.status,
            "applicability": profile.applicability,
            "assets": assets,
        }

    def default_profile_for_contract_type(self, contract_type: str) -> ReviewProfile:
        if contract_type == "procurement_contract":
            return self.get_active_profile(PROCUREMENT_BASIC_PROFILE_ID)
        if contract_type == "service_contract":
            return self.get_active_profile(SERVICE_BASIC_PROFILE_ID)
        return self.get_active_profile(BASIC_PROFILE_ID)

    def summary(self) -> dict:
        active_profiles = self.list_profiles(status="active")
        active_assets = self.list_assets(status="active")
        draft_assets = self.list_assets(status="draft")
        return {
            "active_profiles": len(active_profiles),
            "active_assets": len(active_assets),
            "draft_assets": len(draft_assets),
        }
