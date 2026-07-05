"""Add opencode to model_backends provider CHECK constraint

Revision ID: 0079_add_opencode_provider
Revises: 0078_merge_error_tracking
Create Date: 2026-07-05 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0079_add_opencode_provider"
down_revision: Union[str, None] = "0078_merge_error_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_backends DROP CONSTRAINT IF EXISTS ck_model_backends_provider"
    )
    op.execute(
        "ALTER TABLE model_backends ADD CONSTRAINT ck_model_backends_provider "
        "CHECK (provider IN ("
        "'ai21', 'anthropic', 'azure_openai', 'bedrock', 'cohere', 'custom', "
        "'deepseek', 'fireworks', 'gemini', 'grok', 'groq', 'jan', 'llamacpp', "
        "'lm_studio', 'localai', 'mistral', 'ollama', 'opencode', 'openai', "
        "'openrouter', 'perplexity', 'qwen', 'replicate', 'tgi', 'togetherai', "
        "'vertexai', 'vllm', 'watsonx'"
        "))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE model_backends DROP CONSTRAINT IF EXISTS ck_model_backends_provider"
    )
    op.execute(
        "ALTER TABLE model_backends ADD CONSTRAINT ck_model_backends_provider "
        "CHECK (provider IN ("
        "'ai21', 'anthropic', 'azure_openai', 'bedrock', 'cohere', 'custom', "
        "'deepseek', 'fireworks', 'gemini', 'grok', 'groq', 'jan', 'llamacpp', "
        "'lm_studio', 'localai', 'mistral', 'ollama', 'openai', "
        "'openrouter', 'perplexity', 'qwen', 'replicate', 'tgi', 'togetherai', "
        "'vertexai', 'vllm', 'watsonx'"
        "))"
    )
