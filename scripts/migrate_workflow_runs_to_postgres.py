from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.config import get_settings
from app.services.db_store import PostgresWorkflowRunStore
from app.services.workflow_runs import JsonWorkflowRunStore


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL / CONTRACT_COMPLIANCE_DATABASE_URL is required.", file=sys.stderr)
        return 2

    json_settings = replace(settings, workflow_store_backend="json")
    postgres_settings = replace(settings, workflow_store_backend="postgres")

    json_store = JsonWorkflowRunStore(json_settings.data_dir / "workflow_runs.json")
    postgres_store = PostgresWorkflowRunStore(postgres_settings)

    runs = json_store.load_runs()
    postgres_store.save_runs(runs)
    migrated = postgres_store.load_runs()

    expected = sorted((run.id, run.input_hash, len(run.step_runs)) for run in runs)
    actual = sorted((run.id, run.input_hash, len(run.step_runs)) for run in migrated)
    if expected != actual:
        print("Workflow run mismatch after migration.", file=sys.stderr)
        return 1

    step_count = sum(len(run.step_runs) for run in migrated)
    print(f"Migrated workflow_runs={len(migrated)} step_runs={step_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
