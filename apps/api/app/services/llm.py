from __future__ import annotations

import json
from datetime import datetime, timezone
from time import perf_counter
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
        missing_fields = self._missing_fields()
        if missing_fields:
            return self._build_probe(
                status="not_configured",
                title="模型配置尚不完整",
                detail="补齐缺失项后，页面上的按钮才会真正发起模型连通性检测。",
                configured=False,
                verified=False,
                missing_fields=missing_fields,
            )

        return self._build_probe(
            status="configured",
            title="模型参数已配置",
            detail="为避免无意消耗额度，当前未启用主动探测。点击按钮后才会发起一次最小化检测请求。",
            configured=True,
            verified=False,
        )

    def manual_check(self) -> LLMProbe:
        missing_fields = self._missing_fields()
        if missing_fields:
            return self._build_probe(
                status="not_configured",
                title="模型配置尚不完整",
                detail="缺少必要环境变量，当前没有发起真实探测请求。",
                configured=False,
                verified=False,
                missing_fields=missing_fields,
                checked_at=_utc_now(),
            )

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": "You are a health check assistant. Reply with only OK."},
                {"role": "user", "content": "ping"},
            ],
            "temperature": 0,
            "max_tokens": 8,
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

        started_at = perf_counter()
        try:
            with request.urlopen(req, timeout=self.settings.llm_timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return self._build_probe(
                status="error",
                title="模型探测失败",
                detail=f"模型接口返回 HTTP {exc.code}，请检查 Base URL、模型名和 API Key。",
                configured=True,
                verified=False,
                checked_at=_utc_now(),
                latency_ms=_latency_ms(started_at),
                error_detail=_safe_preview(exc.read().decode("utf-8", errors="ignore")) or str(exc),
            )
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            return self._build_probe(
                status="error",
                title="模型探测失败",
                detail="没有拿到可用响应，请检查网络、服务监听地址或返回格式。",
                configured=True,
                verified=False,
                checked_at=_utc_now(),
                latency_ms=_latency_ms(started_at),
                error_detail=_safe_preview(str(exc)),
            )

        return self._build_probe(
            status="verified",
            title="模型连接正常",
            detail="已完成一次最小化检测请求，当前模型可以正常返回结果。",
            configured=True,
            verified=True,
            checked_at=_utc_now(),
            latency_ms=_latency_ms(started_at),
            response_preview=_extract_preview(response_payload),
        )

    def complete_json(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float = 0,
        max_tokens: int = 1800,
    ) -> dict:
        missing_fields = self._missing_fields()
        if missing_fields:
            raise ValueError(f"LLM 配置不完整：{', '.join(missing_fields)}")

        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        req = request.Request(
            self.chat_completions_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        started_at = perf_counter()
        with request.urlopen(req, timeout=self.settings.llm_timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))

        raw_text = _extract_chat_content(response_payload)
        return {
            "model": self.settings.llm_model,
            "latency_ms": _latency_ms(started_at),
            "raw_text": raw_text,
            "parsed_json": _parse_json_content(raw_text),
        }

    def _missing_fields(self) -> list[str]:
        missing_fields: list[str] = []
        if not self.settings.llm_base_url:
            missing_fields.append("LLM_BASE_URL")
        if not self.settings.llm_api_key:
            missing_fields.append("LLM_API_KEY")
        if not self.settings.llm_model:
            missing_fields.append("LLM_MODEL")
        if not self.chat_completions_url:
            missing_fields.append("LLM_CHAT_COMPLETIONS_URL")
        return missing_fields

    def _build_probe(
        self,
        *,
        status: str,
        title: str,
        detail: str,
        configured: bool,
        verified: bool,
        missing_fields: list[str] | None = None,
        checked_at: str | None = None,
        latency_ms: float | None = None,
        response_preview: str | None = None,
        error_detail: str | None = None,
    ) -> LLMProbe:
        return LLMProbe(
            base_url=self.settings.llm_base_url,
            chat_completions_url=self.chat_completions_url,
            model=self.settings.llm_model,
            status=status,
            title=title,
            detail=detail,
            configured=configured,
            verified=verified,
            api_key_present=bool(self.settings.llm_api_key),
            env_file_path=self.settings.env_file_path,
            missing_fields=missing_fields or [],
            checked_at=checked_at,
            latency_ms=latency_ms,
            response_preview=response_preview,
            error_detail=error_detail,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latency_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 2)


def _extract_preview(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "模型已返回响应，但没有可展示的文本内容。"

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return "模型已返回响应，但返回结构与预期不一致。"

    message = first_choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return _safe_preview(content)

    return "模型已返回响应，但预览内容为空。"


def _extract_chat_content(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM 响应缺少 choices。")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("LLM 响应 choices[0] 结构不正确。")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("LLM 响应缺少 message。")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM 响应内容为空。")
    return content.strip()


def _parse_json_content(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM 响应不是合法 JSON。") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM 响应 JSON 顶层必须是对象。")
    return parsed


def _safe_preview(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    return compact[:limit]
