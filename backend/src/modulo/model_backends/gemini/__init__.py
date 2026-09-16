from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI

from modulo.model_backends.base import (
    HealthResult,
    LangChainChatForwardingMixin,
    ModelBackendBase,
    openai_compatible_health_check,
)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatGoogleGenerativeAI targeting Gemini API."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatGoogleGenerativeAI(
            model=model_id,
            api_key=api_key,
            **default_params,
        )
        self._backend_id = f"gemini/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"GeminiBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=GEMINI_BASE_URL,
            api_key=None,
            extra_headers={"x-goog-api-key": self._api_key},
        )
