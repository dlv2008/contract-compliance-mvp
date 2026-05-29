# Contract Compliance MVP

This repository is the working tree for the `contract-compliance-mvp` demo.

Current focus:

- finalize the business and MVP scope
- develop and demo the product inside local `WSL`
- reuse the existing source-based `RAGFlow` instance instead of rebuilding it with Docker
- keep GitHub CI for code checks, while postponing cloud auto-deploy until hardware is upgraded

Key folders:

- `apps/api`: FastAPI-based demo application
- `docs`: operations notes, local WSL runbooks, and deployment decisions
- `infra`: deployment assets kept for the later cloud phase
- `resource`: Chinese sample contracts, policies, labels, and UI references
- `infra_staging`: one-off scripts and temporary assets used during the 2026-05-28 server cleanup

Deferred public domains after the cloud hardware issue is solved:

- `compliance.trendbot.cn`: main demo site
- `rag.trendbot.cn`: shared RAGFlow entrypoint
