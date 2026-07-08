"""OpenAI-compatible LLM service."""

import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings


class LLMService:
    """Small OpenAI-compatible chat client used by the LangGraph workflow."""

    def __init__(self):
        settings = get_settings()
        self.api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or settings.openai_api_key
        self.base_url = (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or settings.openai_base_url).rstrip("/")
        self.model = os.getenv("LLM_MODEL_ID") or os.getenv("OPENAI_MODEL") or settings.openai_model
        self.timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))

        if not self.api_key:
            print("[WARN] LLM API Key未配置,行程规划将使用规则化备用方案")
        else:
            print("[OK] LLM服务初始化成功")
            print(f"   Base URL: {self.base_url}")
            print(f"   模型: {self.model}")
            print(f"   超时: {self.timeout}s, 重试: {self.max_retries}次")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        response_format: Optional[Dict[str, str]] = None,
    ) -> str:
        """Call an OpenAI-compatible chat completions endpoint."""
        if not self.api_key:
            raise RuntimeError("LLM API Key未配置")

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Connection": "close",
        }

        url = f"{self.base_url}/chat/completions"
        data = self._post_chat_with_retries(url, headers, payload)

        return data["choices"][0]["message"]["content"]

    def chat_json(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> Dict[str, Any]:
        """Call the model and parse the response as JSON."""
        try:
            content = self.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except (httpx.RemoteProtocolError, httpx.TransportError) as exc:
            print(f"[WARN] LLM JSON mode连接失败,降级为普通JSON提示重试: {exc}")
            content = self.chat(
                messages=self._messages_with_json_instruction(messages),
                temperature=temperature,
                response_format=None,
            )
        return self._extract_json(content)

    def _post_chat_with_retries(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        response_format = payload.get("response_format")
        message_chars = sum(len(str(item.get("content", ""))) for item in payload.get("messages", []))

        for attempt in range(1, self.max_retries + 1):
            try:
                print(
                    "[LLM] 请求模型 "
                    f"attempt={attempt}/{self.max_retries}, "
                    f"json_mode={bool(response_format)}, chars={message_chars}"
                )
                with httpx.Client(
                    timeout=httpx.Timeout(self.timeout, connect=15),
                    limits=httpx.Limits(max_keepalive_connections=0),
                    trust_env=False,
                ) as client:
                    response = client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    return response.json()
            except (httpx.RemoteProtocolError, httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    print(f"[WARN] LLM连接异常,准备重试: {exc}")
                    time.sleep(min(0.5 * attempt, 2.0))
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:500] if exc.response is not None else ""
                raise RuntimeError(f"LLM HTTP错误 {exc.response.status_code}: {body}") from exc

        raise last_error or RuntimeError("LLM请求失败")

    @staticmethod
    def _messages_with_json_instruction(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if not messages:
            return messages

        copied = [dict(message) for message in messages]
        copied[-1]["content"] = (
            copied[-1].get("content", "")
            + "\n\n重要: 只输出一个可被 json.loads 解析的 JSON 对象,不要输出 Markdown 或解释。"
        )
        return copied

    @staticmethod
    def _extract_json(content: str) -> Dict[str, Any]:
        text = content.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        if not text.startswith("{") and "{" in text and "}" in text:
            text = text[text.find("{") : text.rfind("}") + 1]

        return json.loads(text)


_llm_instance: Optional[LLMService] = None


def get_llm() -> LLMService:
    """Get the shared LLM service instance."""
    global _llm_instance

    if _llm_instance is None:
        _llm_instance = LLMService()

    return _llm_instance


def reset_llm():
    """Reset the LLM instance for tests or config reloads."""
    global _llm_instance
    _llm_instance = None
