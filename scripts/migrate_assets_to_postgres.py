from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.config import get_settings
from app.services.assets import JsonAssetAuditStore, JsonAssetSourceDocumentStore, JsonAssetStore, JsonLLMExecutionStore
from app.services.db_store import (
    PostgresAssetAuditStore,
    PostgresAssetSourceDocumentStore,
    PostgresAssetStore,
    PostgresLLMExecutionStore,
)


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL / CONTRACT_COMPLIANCE_DATABASE_URL is required.", file=sys.stderr)
        return 2

    json_settings = replace(settings, asset_store_backend="json")
    postgres_settings = replace(settings, asset_store_backend="postgres")

    json_asset_store = JsonAssetStore(json_settings.data_dir / "assets.json")
    json_source_store = JsonAssetSourceDocumentStore(json_settings.data_dir / "asset_source_documents.json")
    json_llm_store = JsonLLMExecutionStore(json_settings.data_dir / "llm_executions.json")
    json_audit_store = JsonAssetAuditStore(json_settings.data_dir / "asset_audit_events.json")

    assets, profiles = json_asset_store.load_state()
    documents = json_source_store.load_documents()
    executions = json_llm_store.load_executions()
    audit_events = json_audit_store.load_events()

    postgres_asset_store = PostgresAssetStore(postgres_settings)
    postgres_source_store = PostgresAssetSourceDocumentStore(postgres_settings)
    postgres_llm_store = PostgresLLMExecutionStore(postgres_settings)
    postgres_audit_store = PostgresAssetAuditStore(postgres_settings)

    postgres_asset_store.save_state(assets, profiles)
    postgres_source_store.save_documents(documents)
    postgres_llm_store.save_executions(executions)
    postgres_audit_store.save_events(audit_events)

    migrated_assets, migrated_profiles = postgres_asset_store.load_state()
    migrated_documents = postgres_source_store.load_documents()
    migrated_executions = postgres_llm_store.load_executions()
    migrated_audit_events = postgres_audit_store.load_events()

    expected_asset_hashes = sorted((asset.id, asset.content_hash) for asset in assets)
    actual_asset_hashes = sorted((asset.id, asset.content_hash) for asset in migrated_assets)
    if expected_asset_hashes != actual_asset_hashes:
        print("Asset hash mismatch after migration.", file=sys.stderr)
        return 1

    print(
        "Migrated assets={assets} profiles={profiles} source_documents={documents} llm_executions={executions} audit_events={audit_events}".format(
            assets=len(migrated_assets),
            profiles=len(migrated_profiles),
            documents=len(migrated_documents),
            executions=len(migrated_executions),
            audit_events=len(migrated_audit_events),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
