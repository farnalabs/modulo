"""DB-level enums shared across models and health/validation surfaces."""

import enum


class ModelBackendProvider(enum.StrEnum):
    """Model backend providers supported by Modulo.

    Members are lowercase strings persisted verbatim on the
    ``ModelBackend.provider`` column (its CHECK constraint is generated from
    these values in ``modulo.db.models.model_backend``), and
    ``modulo.core.validation_level`` keys its baseline map by these members.
    Because stored rows reference the string value, a member's value must not
    be renamed without a data migration.
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    OLLAMA = "ollama"
    CUSTOM = "custom"
    GROQ = "groq"
    DEEPSEEK = "deepseek"
    OPENROUTER = "openrouter"
    TOGETHERAI = "togetherai"
    PERPLEXITY = "perplexity"
    FIREWORKS = "fireworks"
    GROK = "grok"
    QWEN = "qwen"
    AI21 = "ai21"
    VLLM = "vllm"
    TGI = "tgi"
    LM_STUDIO = "lm_studio"
    JAN = "jan"
    LOCALAI = "localai"
    LLAMACPP = "llamacpp"
    COHERE = "cohere"
    MISTRAL = "mistral"
    GEMINI = "gemini"
    OPENCODE = "opencode"
    REPLICATE = "replicate"
    VERTEXAI = "vertexai"
    WATSONX = "watsonx"
