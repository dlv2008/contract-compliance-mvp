from __future__ import annotations

import json
from urllib import error, parse, request

from app.config import Settings, get_settings
from app.models import RagflowProbe


class RagflowClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def probe(self) -> RagflowProbe:
        health_url = f"{self.settings.ragflow_base_url}/v1/system/healthz"
        try:
            with request.urlopen(health_url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return RagflowProbe(
                base_url=self.settings.ragflow_base_url,
                status="http_error",
                detail=f"healthz 返回 HTTP {exc.code}",
                healthy=False,
            )
        except error.URLError as exc:
            return RagflowProbe(
                base_url=self.settings.ragflow_base_url,
                status="unreachable",
                detail=f"无法连接 RAGFlow: {exc.reason}",
                healthy=False,
            )
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            return RagflowProbe(
                base_url=self.settings.ragflow_base_url,
                status="unverified",
                detail=f"RAGFlow 健康检查响应异常：{exc.__class__.__name__}",
                healthy=False,
            )

        datasets: list[str] = []
        if self.settings.ragflow_api_key:
            dataset_names = self._list_datasets()
            if dataset_names is not None:
                datasets = dataset_names
                detail = f"RAGFlow 健康检查通过，可见知识库 {len(datasets)} 个"
            else:
                detail = "RAGFlow 健康检查通过，但未成功读取知识库列表"
        else:
            detail = "RAGFlow 健康检查通过，未配置 API Key，暂不读取知识库列表"

        return RagflowProbe(
            base_url=self.settings.ragflow_base_url,
            status="connected" if payload.get("status") == "ok" else "degraded",
            detail=detail,
            healthy=payload.get("status") == "ok",
            datasets=datasets,
            raw=payload,
        )

    def _list_datasets(self) -> list[str] | None:
        if not self.settings.ragflow_api_key:
            return None

        url = f"{self.settings.ragflow_base_url}/api/v1/datasets?{parse.urlencode({'page': 1, 'page_size': 20})}"
        req = request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.ragflow_api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

        data = payload.get("data", [])
        if isinstance(data, dict):
            data = data.get("items", data.get("datasets", []))
        if not isinstance(data, list):
            return None
        return [item.get("name", item.get("id", "未命名知识库")) for item in data if isinstance(item, dict)]
