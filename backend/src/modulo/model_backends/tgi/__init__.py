from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend


class TgiBackend(OpenAICompatibleBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str = "http://localhost:8080/v1",
        **default_params: Any,
    ):
        super().__init__(
            api_key=api_key or "",
            model_id=model_id,
            base_url=base_url.rstrip("/"),
            provider="tgi",
            **default_params,
        )
