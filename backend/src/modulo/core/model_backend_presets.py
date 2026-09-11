"""Curated provider-preset catalogue for the model-backend quick-start flow.

Presets are static, code-defined data — not DB rows.  Each preset lets a new
user create a working model backend by picking a provider and pasting only an
API key; the default model id is pre-filled.
"""

from typing import Any

from pydantic import BaseModel


class ModelBackendPreset(BaseModel):
    """A single provider preset for the quick-start create flow."""

    id: str
    provider: str
    display_name: str
    default_model_id: str
    description: str
    api_key_docs_url: str
    default_params: dict[str, Any] = {}


MODEL_BACKEND_PRESETS: list[ModelBackendPreset] = [
    ModelBackendPreset(
        id="openai",
        provider="openai",
        display_name="OpenAI",
        default_model_id="gpt-4o",
        description="GPT-4o — fast, multimodal flagship model from OpenAI.",
        api_key_docs_url="https://platform.openai.com/api-keys",
        default_params={"temperature": 0.7},
    ),
    ModelBackendPreset(
        id="anthropic",
        provider="anthropic",
        display_name="Anthropic",
        default_model_id="claude-sonnet-4-20250514",
        description="Claude Sonnet 4 — balanced performance and speed from Anthropic.",
        api_key_docs_url="https://console.anthropic.com/settings/keys",
        default_params={"temperature": 0.7},
    ),
    ModelBackendPreset(
        id="gemini",
        provider="gemini",
        display_name="Google Gemini",
        default_model_id="gemini-2.5-pro",
        description="Gemini 2.5 Pro — Google's most capable multimodal model.",
        api_key_docs_url="https://aistudio.google.com/apikey",
        default_params={"temperature": 0.7},
    ),
    ModelBackendPreset(
        id="deepseek",
        provider="deepseek",
        display_name="DeepSeek",
        default_model_id="deepseek-chat",
        description="DeepSeek Chat — high-quality open-weight model at low cost.",
        api_key_docs_url="https://platform.deepseek.com/api_keys",
    ),
    ModelBackendPreset(
        id="groq",
        provider="groq",
        display_name="Groq",
        default_model_id="llama-3.3-70b-versatile",
        description="Llama 3.3 70B — ultra-fast inference on Groq hardware with a generous free tier.",
        api_key_docs_url="https://console.groq.com/keys",
        default_params={"temperature": 0.6},
    ),
]
