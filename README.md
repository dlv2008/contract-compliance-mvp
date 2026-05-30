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

Repository hygiene:

- one-off server cleanup scripts and staging duplicates are intentionally kept out of Git
- real credentials must stay in local `.env` files or deployment secrets, never in tracked files

Deferred public domains after the cloud hardware issue is solved:

- `compliance.trendbot.cn`: main demo site
- `rag.trendbot.cn`: shared RAGFlow entrypoint
