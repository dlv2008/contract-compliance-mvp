from __future__ import annotations

import json
from urllib import error, request

from app.config import Settings, get_settings
from app.models import LLMProbe


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def chat_completions_url(self) -> str:
        explicit_url = self.settings.llm_chat_completions_url
        if explicit_url:
            return explicit_url.rstrip("/")
        return f"{self.settings.llm_base_url}/chat/completions"

    def probe(self) -> LLMProbe:
        if not self.settings.llm_api_key:
            return self._probe(
                status="missing_api_key",
                detail="未配置 LLM_API_KEY，模型服务不会被调用。",
                configured=False,
                verified=False,
            )

        if not self.settings.llm_probe_enabled:
            return self._probe(
                status="configured",
                detail="模型参数已配置；为避免无意消耗额度，当前未启用主动探测。",
                configured=True,
                verified=False,
            )

        payload = {
            "model": self.settings.llm_model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "max_tokens": 4,
        }
        req = request.Request(
            self.chat_completions_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.settings.llm_timeout_seconds) as response:
                json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return self._probe(
                status="http_error",
                detail=f"模型服务返回 HTTP {exc.code}，请检查路径、模型名和供应商兼容参数。",
                configured=True,
                verified=False,
            )
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            return self._probe(
                status="unverified",
                detail=f"模型服务探测失败：{exc.__class__.__name__}",
                configured=True,
                verified=False,
            )

        return self._probe(
            status="verified",
            detail="模型服务主动探测通过。",
            configured=True,
            verified=True,
        )

    def _probe(
        self,
        status: str,
        detail: str,
        configured: bool,
        verified: bool,
    ) -> LLMProbe:
        return LLMProbe(
            base_url=self.settings.llm_base_url,
            chat_completions_url=self.chat_completions_url,
            model=self.settings.llm_model,
            status=status,
            detail=detail,
            configured=configured,
            verified=verified,
            api_key_present=bool(self.settings.llm_api_key),
        )
