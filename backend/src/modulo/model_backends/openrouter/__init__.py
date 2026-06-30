from typing import Any

from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, _openai_compatible_health_check


class OpenRouterBackend(ModelBackendBase):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            **default_params,
        )
        self._backend_id = f"openrouter/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self):
        return self._backend_id

    def __repr__(self) -> str:
        return f"OpenRouterBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await _openai_compatible_health_check(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._api_key,
        )

    async def invoke(self, messages, **kwargs):
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages, **kwargs):
        return self._model.astream(messages, **kwargs)
