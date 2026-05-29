# RAGFlow Deployment Note

Planned domain:

- `rag.trendbot.cn`

Planned version:

- `v0.25.6` based on the current latest stable GitHub release as of 2026-05-28

Current blocker:

- the official self-hosting prerequisite published by RAGFlow recommends `RAM >= 16 GB`
- the current cloud server has about `7.3 GiB` RAM

Decision for now:

- keep `rag.trendbot.cn` reserved
- do not force a same-host RAGFlow deployment until memory pressure is reviewed
