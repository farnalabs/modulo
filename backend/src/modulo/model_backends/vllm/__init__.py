from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend

DEFAULT_VLLM_BASE_URL = "http://localhost:8000/v1"


class VllmBackend(OpenAICompatibleBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str = DEFAULT_VLLM_BASE_URL,
        **default_params: Any,
    ):
        super().__init__(
            api_key=api_key or "",
            model_id=model_id,
            base_url=base_url.rstrip("/"),
            provider="vllm",
            **default_params,
        )
