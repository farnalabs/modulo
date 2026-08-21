from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend

DEFAULT_JAN_BASE_URL = "http://localhost:1337/v1"


class JanBackend(OpenAICompatibleBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str = DEFAULT_JAN_BASE_URL,
        **default_params: Any,
    ):
        super().__init__(
            api_key=api_key or "",
            model_id=model_id,
            base_url=base_url.rstrip("/"),
            provider="jan",
            **default_params,
        )
