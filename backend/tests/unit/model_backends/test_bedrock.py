"""Unit tests for BedrockBackend adapter."""

from unittest.mock import patch

from farna.model_backends.bedrock import BedrockBackend


def test_default_region() -> None:
    with patch("farna.model_backends.bedrock.ChatBedrock") as mock_chat:
        BedrockBackend(
            aws_access_key_id="AKIA123",
            aws_secret_access_key="secret123",
            model_id="us.anthropic.claude-3-haiku-20240307",
        )
    mock_chat.assert_called_once()
    _call_kwargs = mock_chat.call_args.kwargs
    assert _call_kwargs["region_name"] == "us-east-1"
