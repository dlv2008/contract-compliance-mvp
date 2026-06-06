from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import ProfileDryRunRecord, ReviewProfile
from app.services.assets import AssetNotFoundError, AssetRegistry
from app.services.review_engine import analyze_contract
from app.services.workflow_runs import WorkflowRunRepository


class ProfileDryRunError(ValueError):
    pass


class ProfileDryRunStore(Protocol):
    def load_records(self) -> list[ProfileDryRunRecord]:
        pass

    def save_records(self, records: list[ProfileDryRunRecord]) -> None:
        pass


class JsonProfileDryRunStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_records(self) -> list[ProfileDryRunRecord]:
        if not self.state_path.exists():
            self.save_records([])
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [ProfileDryRunRecord.model_validate(item) for item in payload.get("records", [])]
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ProfileDryRunError("Profile dry-run store is unreadable.") from exc

    def save_records(self, records: list[ProfileDryRunRecord]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        payload = {"records": [record.model_dump() for record in records]}
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)


class ProfileDryRunService:
    def __init__(
        self,
        settings: Settings | None = None,
        registry: AssetRegistry | None = None,
        store: ProfileDryRunStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or AssetRegistry(self.settings)
        self.store = store or JsonProfileDryRunStore(self.settings.data_dir / "profile_dry_runs.json")

    def list_records(self, profile_id: str | None = None, *, limit: int = 20) -> list[ProfileDryRunRecord]:
        records = self.store.load_records()
        if profile_id:
            records = [record for record in records if record.profile_id == profile_id]
        return sorted(records, key=lambda item: item.created_at, reverse=True)[:limit]

    def get_record(self, record_id: str) -> ProfileDryRunRecord:
        record = next((item for item in self.store.load_records() if item.id == record_id), None)
        if record is None:
            raise ProfileDryRunError("Profile dry-run record does not exist.")
        return record

    def latest_record(self, profile_id: str) -> ProfileDryRunRecord | None:
        records = self.list_records(profile_id, limit=1)
        return records[0] if records else None

    def publication_gate_status(self, profile: ReviewProfile | str) -> dict:
        resolved_profile = self.registry.get_profile(profile) if isinstance(profile, str) else profile
        latest = self.latest_record(resolved_profile.id)
        status = {
            "allowed": False,
            "reason": "No dry-run record exists for this review profile.",
            "reason_code": "missing_dry_run",
            "latest_record": latest.model_dump() if latest else None,
        }
        if latest is None:
            return status
        if latest.profile_version != resolved_profile.version:
            status["reason"] = "Latest dry-run belongs to a different profile version."
            status["reason_code"] = "version_mismatch"
            return status
        current_signature = _profile_snapshot_signature(self.registry.freeze_profile(resolved_profile))
        latest_signature = _profile_snapshot_signature(latest.task_snapshot.get("selected_profile_snapshot", {}))
        if latest_signature and current_signature != latest_signature:
            status["reason"] = "Latest dry-run does not match the current profile asset snapshot."
            status["reason_code"] = "stale_dry_run"
            return status
        if _is_before(latest.created_at, resolved_profile.updated_at):
            status["reason"] = "Latest dry-run is older than the latest profile change."
            status["reason_code"] = "stale_dry_run"
            return status
        status["allowed"] = True
        status["reason"] = "Latest dry-run is valid for this profile version."
        status["reason_code"] = "passed"
        return status

    def assert_profile_can_publish(self, profile: ReviewProfile | str) -> None:
        status = self.publication_gate_status(profile)
        if not status["allowed"]:
            raise ProfileDryRunError(status["reason"])

    def run(
        self,
        profile_id: str,
        *,
        contract_name: str | None,
        source_filename: str,
        source_text: str,
        actor: str = "reviewer",
    ) -> ProfileDryRunRecord:
        normalized_text = source_text.replace("\r\n", "\n").strip()
        if not normalized_text:
            raise ProfileDryRunError("Dry-run contract text cannot be empty.")
        try:
            profile = self.registry.get_profile(profile_id)
        except AssetNotFoundError as exc:
            raise ProfileDryRunError(str(exc)) from exc
        dry_run_id = f"dry-run-{uuid.uuid4().hex[:10]}"
        task = analyze_contract(
            task_id=dry_run_id,
            source_filename=source_filename or "dry-run.txt",
            contract_name=contract_name,
            contract_text=normalized_text,
            rule_context=self.registry.rule_context_for_profile(profile),
        )
        task = task.model_copy(
            update={
                "selected_profile_id": profile.id,
                "selected_profile_name": profile.name,
                "selected_profile_snapshot": self.registry.freeze_profile(profile),
            }
        )
        WorkflowRunRepository(self.settings).record_from_task(task, run_type="profile_dry_run")
        record = self._build_record(dry_run_id, profile, task, source_filename, actor)
        records = [item for item in self.store.load_records() if item.id != record.id]
        records.append(record)
        records = sorted(records, key=lambda item: item.created_at, reverse=True)[:200]
        self.store.save_records(records)
        return record

    def _build_record(
        self,
        record_id: str,
        profile: ReviewProfile,
        task: Any,
        source_filename: str,
        actor: str,
    ) -> ProfileDryRunRecord:
        semantic_trace = next((event for event in task.agent_trace if event.type == "semantic.evaluate"), None)
        semantic_payload = semantic_trace.payload if semantic_trace else {}
        return ProfileDryRunRecord(
            id=record_id,
            profile_id=profile.id,
            profile_name=profile.name,
            profile_version=profile.version,
            profile_status=profile.status,
            contract_name=task.name,
            source_filename=source_filename or task.source_filename,
            created_by=actor or "reviewer",
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            overall_risk=task.overall_risk,
            overall_risk_label=task.overall_risk_label,
            status=task.status,
            status_label=task.status_label,
            decision=task.decision,
            decision_label=task.decision_label,
            risk_count=len(task.risks),
            high_risk_count=sum(1 for risk in task.risks if risk.level == "high"),
            medium_risk_count=sum(1 for risk in task.risks if risk.level == "medium"),
            semantic_rule_count=int(semantic_payload.get("semantic_rule_count") or 0),
            semantic_hit_count=int(semantic_payload.get("hit_count") or 0),
            warning_count=int(semantic_payload.get("warning_count") or 0),
            task_snapshot=task.model_dump(),
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_before(left: str | None, right: str | None) -> bool:
    left_dt = _parse_datetime(left)
    right_dt = _parse_datetime(right)
    if left_dt is None or right_dt is None:
        return False
    return left_dt < right_dt


def _profile_snapshot_signature(snapshot: dict) -> str:
    assets = snapshot.get("assets") if isinstance(snapshot, dict) else None
    if not isinstance(assets, list):
        return ""
    normalized = [
        {
            "asset_id": item.get("asset_id"),
            "asset_type": item.get("asset_type"),
            "version": item.get("version"),
            "content_hash": item.get("content_hash"),
        }
        for item in assets
        if isinstance(item, dict)
    ]
    normalized.sort(key=lambda item: (item.get("asset_type") or "", item.get("asset_id") or ""))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
