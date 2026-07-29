from modulo.model_backends.module import OpenAICompatibleBackend


class Ai21Backend(OpenAICompatibleBackend):
    def __init__(self, api_key: str, model_id: str, **default_params):
        super().__init__(
            api_key=api_key,
            model_id=model_id,
            base_url="https://api.ai21.com/studio/v1",
            provider="ai21",
            **default_params,
        )
