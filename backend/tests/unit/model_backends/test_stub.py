import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from modulo.model_backends.stub import StubModelBackend, UnexpectedInputError, normalize_input


def test_is_a_langchain_chat_model() -> None:
    assert isinstance(StubModelBackend(), BaseChatModel)


@pytest.mark.asyncio
async def test_ainvoke_returns_ai_message_from_normalized_fixture() -> None:
    backend = StubModelBackend({"Follow   the rules.\nSummarize this": "A short summary."})

    result = await backend.ainvoke(
        [
            SystemMessage(content="Follow the rules."),
            HumanMessage(content="  Summarize\nthis  "),
        ]
    )

    assert isinstance(result, AIMessage)
    assert result.content == "A short summary."


@pytest.mark.asyncio
async def test_ainvoke_raises_for_unmapped_input() -> None:
    backend = StubModelBackend({"known input": "known response"})

    with pytest.raises(UnexpectedInputError, match="unmapped input") as error:
        await backend.ainvoke([HumanMessage(content="unmapped   input")])

    assert error.value.normalized_input == "unmapped input"


def test_normalize_input_extracts_text_blocks() -> None:
    message = HumanMessage(content=[{"type": "text", "text": "hello"}, " world "])

    assert normalize_input([message]) == "hello world"


def test_conflicting_normalized_fixture_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="Conflicting fixtures"):
        StubModelBackend({"same input": "first", "same   input": "second"})
