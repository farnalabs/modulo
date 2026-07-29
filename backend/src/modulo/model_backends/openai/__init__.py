__all__ = ["OpenAIBackend"]

from langchain_openai import ChatOpenAI  # noqa: F401

from modulo.model_backends.module import OpenAICompatibleBackend as OpenAIBackend
