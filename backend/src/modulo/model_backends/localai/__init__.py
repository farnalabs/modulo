from typing import Any

from langchain_openai import ChatOpenAI  # noqa: F401

from modulo.model_backends.module import OpenAICompatibleBackend

DEFAULT_LOCALAI_BASE_URL = "http://localhost:8080/v1"


class LocalAIBackend(OpenAICompatibleBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str = DEFAULT_LOCALAI_BASE_URL,
        **default_params: Any,
    ):
        super().__init__(
            api_key=api_key or "",
            model_id=model_id,
            base_url=base_url.rstrip("/"),
            provider="localai",
            **default_params,
        )
