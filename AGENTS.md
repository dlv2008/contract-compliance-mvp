# Project Memory

## Runtime Environment

- The project lives in Ubuntu/WSL at:
  - `/home/sdt00157/contract-compliance-mvp`
  - Shell shortcut: `~/contract-compliance-mvp`
- Run commands inside Ubuntu/WSL, not from a Windows-side project path.
- The FastAPI app lives at:
  - `~/contract-compliance-mvp/apps/api`
- The Python runtime is:
  - `~/contract-compliance-mvp/.venv/bin/python`

## Standard Dev Server Command

The user normally runs the API from an Ubuntu terminal with:

```bash
cd ~/contract-compliance-mvp/apps/api
../../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 18080
```

The default app URL is:

```text
http://127.0.0.1:18080/
```

Health check:

```bash
curl http://127.0.0.1:18080/api/health
```

## Agent-Managed Server Testing

When Codex needs to start or restart the dev server itself, use the Ubuntu user-systemd helper:

```bash
cd ~/contract-compliance-mvp
scripts/dev_api_ubuntu_service.sh restart
scripts/dev_api_ubuntu_service.sh health
scripts/dev_api_ubuntu_service.sh status
```

This starts the same app with the same Ubuntu venv on port `18080`.

If the user already has `18080` running and Codex should not disturb it, use a temporary port:

```bash
cd ~/contract-compliance-mvp
CONTRACT_COMPLIANCE_API_UNIT=contract-compliance-api-test \
CONTRACT_COMPLIANCE_API_PORT=18081 \
scripts/dev_api_ubuntu_service.sh restart
```

Stop the temporary service after validation:

```bash
CONTRACT_COMPLIANCE_API_UNIT=contract-compliance-api-test \
CONTRACT_COMPLIANCE_API_PORT=18081 \
scripts/dev_api_ubuntu_service.sh stop
```

## Local Services

- Contract compliance API: `http://127.0.0.1:18080`
- RAGFlow API: `http://127.0.0.1:9380`
- RAGFlow health endpoint:

```bash
curl http://127.0.0.1:9380/v1/system/healthz
```

Expected RAGFlow health includes:

```json
{"db":"ok","doc_engine":"ok","redis":"ok","status":"ok","storage":"ok"}
```

## Verification Commands

Run tests from Ubuntu:

```bash
cd ~/contract-compliance-mvp/apps/api
. ../../.venv/bin/activate
pytest -q
ruff check app tests
```

Do not rely on a Windows-side background process to host uvicorn. If a long-running API process is needed, use the user's Ubuntu terminal or `scripts/dev_api_ubuntu_service.sh`.
