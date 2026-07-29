from modulo.model_backends.module import OpenAICompatibleBackend


class PerplexityBackend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url="https://api.perplexity.ai",
            provider="perplexity",
            **default_params,
        )
