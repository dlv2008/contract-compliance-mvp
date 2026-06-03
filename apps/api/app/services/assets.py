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
            "policy_reference": "采购付款制度：预付款原则上不得超过合同总价 30%。",
        },
        schema_version="hard-rule-v1",
        description="当预付款比例超过阈值时命中风险。",
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
        description="收款账户主体与签约相对方不一致时命中风险。",
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
            _ref("asset-hardrule-prepay-v1", "采购预付款比例控制。"),
            _ref("asset-hardrule-payee-v1", "收款主体一致性控制。"),
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
            _ref("asset-hardrule-prepay-v1"),
            _ref("asset-hardrule-payee-v1"),
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
            _ref("asset-hardrule-prepay-v1"),
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
            drafts.append(
                self.create_asset_draft(
                    asset_type="hard_rule",
                    name=f"制度草稿：预付款比例超过 {threshold}% 控制",
                    applicability={"contract_type": contract_type},
                    content={
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
        for ref in profile.assets:
            asset = next((item for item in assets if item.id == ref.asset_id), None)
            if not asset or asset.asset_type != "hard_rule" or asset.status != "active":
                continue
            if asset.content.get("fact_key") == "payment.prepay_ratio" and asset.content.get("operator") == ">":
                threshold = float(asset.content.get("value") or threshold)
                source_asset_id = asset.id
        return {
            "prepay_threshold": threshold,
            "prepay_threshold_asset_id": source_asset_id,
        }

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
        asset_ids = {asset.id for asset in assets}
        for seed in SEED_ASSETS:
            if seed.id not in asset_ids:
                assets.append(seed)
                changed = True
        profile_ids = {profile.id for profile in profiles}
        for seed in SEED_PROFILES:
            if seed.id not in profile_ids:
                profiles.append(seed)
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
        return (
            existing.content.get("fact_key") == new_asset.content.get("fact_key")
            and existing.content.get("operator") == new_asset.content.get("operator")
        )


def asset_counts(profile: ReviewProfile) -> dict[str, int]:
    counts: dict[str, int] = {}
    for asset in profile.assets:
        counts[asset.asset_type] = counts.get(asset.asset_type, 0) + 1
    return counts
