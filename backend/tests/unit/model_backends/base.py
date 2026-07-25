from unittest.mock import patch


def assert_chat_model_base_url(
    module_name: str,
    chat_class_name: str,
    backend_class: type,
    expected_url: str,
    *,
    api_key: str = "test-key",
    model_id: str = "test-model",
    **extra_expected: object,
) -> None:
    with patch(f"modulo.model_backends.{module_name}.{chat_class_name}") as mock:
        backend_class(api_key=api_key, model_id=model_id)
        mock.assert_called_once_with(
            model=model_id,
            api_key=api_key,
            base_url=expected_url,
            **extra_expected,
        )
