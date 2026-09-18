from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend

DEFAULT_LM_STUDIO_BASE_URL = "http://localhost:1234/v1"


class LmStudioBackend(OpenAICompatibleBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str = DEFAULT_LM_STUDIO_BASE_URL,
        **default_params: Any,
    ):
        super().__init__(
            api_key=api_key or "",
            model_id=model_id,
            base_url=base_url.rstrip("/"),
            provider="lm_studio",
            **default_params,
        )
