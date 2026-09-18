from typing import Any

from modulo.model_backends.module import OpenAICompatibleBackend

PERPLEXITY_BASE_URL = "https://api.perplexity.ai"


class PerplexityBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params: Any):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url=PERPLEXITY_BASE_URL,
            provider="perplexity",
            **default_params,
        )
