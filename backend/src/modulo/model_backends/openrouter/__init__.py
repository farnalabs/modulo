from typing import Any

from langchain_openai import ChatOpenAI  # noqa: F401

from modulo.model_backends.module import OpenAICompatibleBackend


class OpenRouterBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            **default_params,
        )
